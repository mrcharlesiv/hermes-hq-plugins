# hermes-hq-proactive (Beta)

> **Beta, off by default.** While it is off, nothing is added to your chats, the bot has none of its tools, nothing is written, and no checks run.

A [Hermes](https://github.com/NousResearch/hermes-agent) plugin that keeps track of what is left open in your chats with a bot and suggests the next step. It has its own **Proactivity (Beta)** page in Hermes Desktop and in the Hermes HQ app (iPhone and Mac).

It only reads and suggests. Nothing is sent, booked, paid, posted or deleted without you.

## What it does

- **Keeps track of what is left open.** Things you say you will do ("I'll send the lease back by Friday"), things you are waiting on ("still waiting on the quote"), and things the bot says it will come back to ("I'll check again on Friday"). These are kept apart from your task list.
- **Closes them when they get done.** The hourly check reads what is new in your chats and marks an item done when the chat shows it was. You can also close, snooze, edit, drop or reopen one on the page.
- **Suggests the next step** when something is due, past its date, or has had no news for a few days.
- **Puts suggestions where you already look.** They go to **Tasks › Ideas** in the Hermes HQ app. **Add** puts one on your task list, **Add & Start** has the bot do it, **Dismiss** drops it for good. Without the Hermes HQ add-on (`hermes-hq-push` 0.9.90 or newer), or with Ideas turned off, they stay on the Proactivity page.
- **Posts a short brief each morning** in the bot's own chat: what is open and what was suggested. On a day with nothing to report, nothing is posted.
- **Goes one step further in a chat.** When you are talking to the bot, it is asked to do the one obvious read-only check and tell you what it found, instead of only suggesting it.
- **Notices requests you repeat** and suggests making one a skill or a scheduled job.
- **Learns from your answers.** What you keep dismissing is suggested less, and a kind you dismiss three times in a row is paused for a week. A suggestion you never looked at counts for nothing, and a raised bar eases back down over time.

## Which chats it reads

Only your own one-to-one chats with the bot: the app, the desktop, the terminal, and a direct message on a messenger. It never reads, and never adds anything to:

- group chats and channels (other people read the reply),
- scheduled jobs and their results,
- helper agents and other background runs.

A chat it cannot place counts as a group on a messenger and as your own on your machine, so a mistake never puts a private note in a group. A chat is read once it has been quiet for 20 minutes, each chat from where the last check stopped. Secrets are masked before any text reaches a model.

## What it costs

The hourly check uses no model when nothing is new. When there are new chat messages it makes one small call to read them (two at most per check). A suggestion that goes to Tasks › Ideas has no draft written for it, because the bot does the work when you tap Add & Start. Limits are checked before anything is drafted, so a check never pays for a draft it then throws away. Turn "Let the check use a model" off and it runs on fixed rules alone, at no cost. You can point the check at a small model with the `proactive_sweep` auxiliary task.

## The three modes

| Mode | What it does |
| :--- | :--- |
| `observe` | Only keeps track and writes the brief. |
| `prep` (default) | Also makes suggestions, within your daily limit. |
| `full` | The same as `prep`, and a suggestion that is past its date is marked **Time-sensitive** and listed first. Never during quiet hours. |

No mode sends or changes anything by itself, and none of them sends a phone alert of its own.

## Turning it on and off

- **On the page:** open **Proactivity (Beta)** and use the switch.
- **In a chat:** `/proactivity on`, `/proactivity off`, `/proactivity` (status), `/proactivity mode prep`, `/proactivity brief`, `/proactivity loops`, `/proactivity check`, `/proactivity reset`.
- **In a terminal:** `hermes proactivity on` and the same words as above. (`hermes proactive …` from 0.2.0 still works.)

It is set per bot. Install and turn it on for each bot you want it on.

Times (quiet hours, the brief hour, "by tonight") are read on the clock Hermes is set to (`timezone:` in `config.yaml`), or the machine's clock when Hermes has none.

## Safety

- The plugin's own checks read chats and write only to its own database and to Tasks › Ideas. They have no tools.
- The morning brief is the one run where a bot has tools. That run is handed the brief ready-made and is held to reading: a tool that writes, sends, pays or deletes is refused in code.
- A suggestion whose wording claims it will act by itself ("sends it for you", "paid automatically") is refused.
- On a turn you typed, the plugin never blocks or changes what the bot may do.

## What changed in 0.3.0

0.2.0 was reviewed end to end; this version is the result.

- Suggestions now reach you: Tasks › Ideas and a morning message, instead of a page nobody was told to open. Approve there did nothing; Add & Start is the real thing.
- Chats are read in full, chat by chat. 0.2.0 read 8 messages of a chat and then moved one bookmark past everything else, skipped every chat that continued an earlier one, read chats you were still typing in, and lost the text when the model call failed.
- Items close: from your chats, from Ideas, and by hand, with Reopen. The bot's own promises are tracked.
- Limits are checked before a draft is paid for (six due items used to cost six model calls for one suggestion), and a draft no longer makes a stale item look fresh.
- A suggestion you never saw no longer counts against its kind. In 0.2.0 that slowly shut every kind off.
- Dates are read on your clock. "Tonight" used to be 11 am Central and so already overdue, and any item with a date was treated as urgent.
- Scheduled jobs, helper agents and group chats are left alone.
- The read-only fence works. In 0.2.0 it was tied to a flag that was only set while no tool could run, and the tool arguments were read under the wrong name.
- The page is in plain words, a cleared number field is no longer saved as 0, "Copy Draft" only marks done when the copy worked, items can be edited, dropped items can be found, and "Check Now" no longer times out on a phone.
- `/proactivity` and `hermes proactivity` are one set of words, with `mode`.

### 0.3.0-beta.2

A pass over how the page looks, on an iPhone and a Mac, light and dark.

- The tabs and the filters stay on one row and slide sideways on a narrow phone.
- No stock "Quick checklist" under a suggestion. Without a model there is a draft only where there is something to draft.
- The brief says "you said you would" and "waiting on someone", and gives the day as "Fri, Nov 27".
- History lists a kind only once you have answered one of its suggestions, and a settings change reads "Proactivity on", not `enabled=True`.
- An item the bot said it would do carries one label, not two.

### 0.3.0-beta.3

- An item that is done or dropped takes its suggestion with it, off the page and out of Tasks › Ideas. Found on the first live check: the chats closed an item a few seconds after a suggestion for it went to Ideas, and the suggestion stayed.

## Testing

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest -q -p no:cacheprovider hermes-hq-proactive/tests
node --check hermes-hq-proactive/dashboard/dist/index.js
node --test hermes-hq-proactive/tests/test_dashboard_ui.mjs
```

The Python tests run in a throwaway `HERMES_HOME`. With a Hermes checkout on the machine they run against Hermes's real scheduler and settings code inside that throwaway home.
