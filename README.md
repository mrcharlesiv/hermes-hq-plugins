# Hermes HQ plugins for Hermes

Plugins that connect a [Hermes](https://github.com/NousResearch/hermes-agent) gateway to **Hermes HQ**, the iPhone
app for Hermes.

## hermes-hq-push: lock-screen notifications

When a bot finishes a reply, or needs your approval to run a command, Hermes HQ shows it on your lock screen, even
when the app isn't open. Approvals carry **Approve once** and **Deny** buttons that work from the lock screen.

Without this plugin, Hermes HQ can only alert you while it's open, or for about 20 seconds after you leave it.

From version 0.6 it also adds a **Hermes HQ theme** to the Hermes desktop app on the same computer: iOS colours, blue
message bubbles and round shapes, in light and dark. It only adds the choice. To use it, open Settings › Appearance ›
Theme in the desktop app and pick **Hermes HQ**. (It writes one file, `desktop-plugins/hermes-hq-theme/plugin.js` in
your Hermes folder, when Hermes loads the plugin.)

From version 0.8 it also keeps Hermes HQ's **performance log** on the same computer, so slow or jumpy moments on the
phone can be looked into later: timings and connection events (when the app's connections opened and closed, how long
pages took, when the screen stalled), never message text. The phone sends them with your normal sign-in, and the plugin
writes them to `hq-logs/<phone>/<date>.jsonl` in your Hermes folder (at most 16 MB a day and 14 days per phone). Nothing
goes anywhere else. The app also checks itself (a reply stuck on "Working…", a card left up, a message shown twice, a
screen that stopped taking taps) and logs what it finds, still without any content. When you tap **Mark a Janky
Moment** (or shake the phone), it adds the minute's frame times and the screen's layout as plain boxes (no text) in
`hq-logs/<phone>/moments/`. Turn it off in Hermes HQ under **Settings › About › Send Performance Logs to Your Computer**.

From version 0.9 it can also alert you about your bots' chats in **Telegram, Discord and other apps**: a reply there,
or an approval a bot is waiting on. It's off until you ask for it. In Hermes HQ, open the Sessions tab's Filter menu
and turn on **Show › Outside Chats**, then check that **Settings › Notifications › Outside Chats** is on. These alerts
have no Approve or Deny buttons, so answer those approvals in the app the chat is in.

From version 0.9.1 it also fixes **Delete, Archive and Rename** when you sign in to Hermes with your Nous account. Hermes
stamps each chat you start with your sign-in but leaves out where the sign-in came from, then refuses your own edits
(`permission_denied`). The plugin lets your Nous sign-in, and only that sign-in, change the chats it made. It stops
itself once Hermes fixes this. It works only after the **messaging gateway** restarts (`hermes gateway restart`).

From version 0.9.2 it also tells Hermes HQ which **scheduled jobs a bot is still working on**. A job that sends its
output to a bot's Bot Chat finishes its own run as soon as it hands the output over, while the bot goes on reading it
and acting on it, often for minutes. With 0.9.2 the Jobs page says **Running** until the bot is done. It reads the
handoff receipts Hermes already keeps in each bot's `runtime/bot_live_delivery` folder and changes nothing. It works
after `hermes serve` or `hermes dashboard` restarts.

From version 0.9.3 it can also hand over the chats made with the old **password sign-in** (Hermes stamps them
`basic:<username>`, often each bot's Bot Chat). Only that password sign-in owns them, so once it's turned off nothing
can delete, archive or rename them. The plugin never guesses who typed that password: on a gateway several people sign
in to, that would give the chats to all of them. Instead you name the one sign-in that takes them over, in your
`~/.hermes/config.yaml`:

```yaml
hermes_hq:
  password_chats_owner: nous:<your Nous user id>
```

(or set `HERMES_HQ_PASSWORD_CHATS_OWNER`). Your Nous user id is on every chat you've made since you switched to Nous
(the `user_id` column of a bot's `state.db` reads `nous:<id>`); your Hermes bot can look it up for you. Then run
`hermes gateway restart`. The first time that sign-in changes one of those chats, the
chat becomes its own, as Hermes does with any old chat, so the old password sign-in can't change it after that. Without
the setting, nothing changes. Turning the password sign-in back on (`dashboard.basic_auth`) and signing in with it
also works, with no plugin at all.

From version 0.9.4 Hermes HQ can send a bot's **answer back to the Telegram or Discord chat** a conversation came
from. A chat under Sessions › Other Apps continues in the app when you write in it, so until now the bot answered in
the app only. Turn on **Also Send Answers to Telegram** (or Discord) in that chat's ⋯ menu, and after each answer the
plugin posts it in the chat, as the bot, with the bot's own token. Only the answer goes: your own message stays in the
app. It never posts on its own: it's off for every chat until you turn it on, a group asks you first and names the
group, and the plugin posts only the newest answer of that one chat, only once, and only when the message it answers
was written in the app. Files in an answer aren't sent. It works after `hermes serve` or `hermes dashboard` restarts.

From version 0.9.5 it also alerts your phone when a bot **waits for you to type something**: a login to save, a
one-time code, a password manager's master password, a skill's API key, your computer password for `sudo`, or a
question it asked. The alert names the bot and what it needs ("Wants you to save a login for online.adp.com"), never
the value, and goes away once the prompt is answered, skipped or times out. Tap it to open the chat and answer. Hermes
announces logins, codes, unlocks and API keys to plugins only from the `feat/prompt-request-hook` change on;
an older Hermes alerts for `sudo` and questions only. It works after `hermes serve` restarts.

From version 0.9.6 the app can show **Working…** in a chat while the gateway runs a turn there that the app didn't
start: another bot's message, a job's hand-off, or a Telegram message. It asks the plugin which chats Hermes's own
ledger says are queued or running (`GET /api/plugins/hermes-hq-push/sessions/working`); the plugin only reads Hermes's
session database and returns chat ids and a status, never message text. Without it, Hermes HQ build 412 and later
guess from the chat's saved steps instead. It works after `hermes serve` restarts.

Version 0.9.7 keeps the app's log lines whole: 0.9.6 and older cut any line holding a `/` at its first `?` or `#`, so
most request and turn lines in `~/.hermes/hq-logs` lost their ends. Now only a path's own query is dropped.

Version 0.9.8 alerts your phone when a bot waits for your approval in a Bot Chat (a message another bot sent it). That
approval waits in the messaging gateway, where nobody was watching, so it used to sit unseen until it timed out. The
alert names the bot and the command; it has no Approve or Deny buttons. Like every alert, it only comes from a bot that
has the plugin turned on (`plugins.enabled` in that bot's `config.yaml`). It works after `hermes gateway restart`.

Version 0.9.9 sends no alert for a bot's own way of saying nothing, such as "[Notification processed; no additional
user-facing response.]" or "(no user-facing response)", the same as a bare `[SILENT]`. Hermes HQ already hid these
lines in the chat. It works after `hermes gateway restart`.

Version 0.9.11 lets the **Hermes desktop app** open the chats you started in Hermes HQ. Hermes stamps each chat with the
sign-in that made it. The desktop app refused chats stamped with a password sign-in, a self-hosted sign-in, or a Nous
sign-in from Hermes HQ before build 402, and said "Resume failed: not_found". The plugin lets two openers past that
stamp, and changes nothing else: the desktop app on the computer that runs the gateway, signed in as that computer's
user, and the very sign-in that made the chat. Opening a chat in the desktop app makes it the desktop's, as Hermes does
with every older chat; Hermes HQ keeps using it as before. It stops itself once Hermes fixes this. It works after
`hermes gateway restart`.

Version 0.9.12 has a bot in a Hermes HQ chat ask a question it needs answered on a **question card**, rather than ending
its reply with the question in text. You answer the card with one tap, or type your own answer. Only questions the bot
needs answered before it can go on get a card (which of two things you meant, a choice that changes what it does next).
"Want me to also…?" offers at the end of finished work stay as text. Bots without the question tool (`clarify`), and
messages from scheduled jobs or other bots, are left alone. The plugin adds the request to your message for the bot
only; the chat still shows what you typed. To turn it off for every bot on the computer, create an empty file named
`ask-cards.off` in `~/.hermes/plugin-data/hermes-hq-push/`. It works after `hermes serve` restarts.

Version 0.9.13 keeps the **Sessions list fast** on a bot that has run many scheduled jobs. To show one page of chats,
Hermes read the first message of every chat the bot ever had, and each scheduled run is a chat: on a bot with 8,700
runs whose prompts were long, one list took 2 seconds, and Hermes HQ asks three times per bot on every refresh. The
plugin has Hermes read those first messages only for the chats on the page. The list itself is unchanged. It stops
itself once Hermes picks the page first. It works after `hermes serve` restarts.

Version 0.9.14 keeps one **looping log line** from filling a phone's daily log. Hermes HQ's performance log on your
computer stops at 16 MB a day; a phone stuck repeating the same line could reach that by morning, and every later line
was lost. Past 1,000 copies of the very same line in a day, the plugin writes one short line with how many it left out
instead. Ordinary days never reach that. It works after `hermes serve` restarts.

Version 0.9.15 has **bots answer right away after Hermes restarts**. A freshly restarted `hermes serve` used to load
a bot only when the first chat with it opened, so that chat sat on "Waking up…" while the bot's plugins loaded (up to
a couple of minutes on a busy computer). Now, 20 seconds after it starts, `hermes serve` loads each of your bots in
the background, one at a time. A computer with one bot has nothing to load. If `hermes serve` freezes for more than
3 seconds, the plugin writes what every part of it was doing to `logs/loop-stall-stacks.log` in your Hermes folder,
so the cause can be found. To turn the background loading off, create an empty file named `warmup.off` in the
plugin's data folder (`plugin-data/hermes-hq-push`). It works after `hermes serve` restarts.

Version 0.9.16 lets you **send from Hermes HQ into a chat the Hermes desktop app has open**. Hermes lets only one
program write to a chat at a time, and a chat open in the desktop app keeps that right for as long as it stays open,
so Hermes HQ's messages were refused ("This chat is open in another Hermes window"). Now your message goes to the
gateway that has the chat open, runs there after anything already running, and the reply comes back to Hermes HQ. A chat
open in a terminal or another program still says it's open elsewhere. This version also fixes Hermes's hand-off of a
message to the gateway on long chats: it gave up on any chat whose history was over 1 MB, so a message sent while a
bot was working in a long chat waited for the bot to finish instead of joining its queue. It works after `hermes
serve` restarts.

Version 0.9.17 lets **a bot ask you for a login, a one-time code or a password manager unlock when another bot's
message started its turn**. Those turns run in the bot's messaging gateway, which has no way to show the secure card,
so Hermes refused: "this session cannot ask the user for a login". Now the request shows as the same card in Hermes
HQ's copy of that chat, and your phone gets an alert while the bot waits. If the chat isn't open in Hermes HQ, the card
appears when you open it, for as long as the bot waits (10 minutes for a login, 5 for a code). What you type goes
straight to the vault, never into the chat. A scheduled job's own run still can't ask anyone. Every bot needs its own
copy of this version, and it works after Hermes restarts.

Version 0.9.18 makes **a bot name the site when it asks for a login or a code**. Without a site, Hermes asked for
whichever browser tab was in front; when the bot's own page hadn't loaded, the card asked for some other site's login.
Now the bot is told to say which site, and Hermes only asks when that site's page is open.

Version 0.9.19 adds **Device Access**: a bot can look someone up in your phone's contacts. In Hermes HQ, open
**Settings › Device Access** and switch on the details you'll share (phone numbers, email addresses, birthdays, postal
addresses); everything starts off. A bot asks by name, email address or phone number, the plugin wakes your phone with
a silent notification, and the phone answers with at most 10 matches carrying only the details you switched on. Bots
never get the whole address book, and the phone checks every request against its own switches. Bots see the
`device_contacts_search` tool only while a phone shares something. If the phone hasn't answered within 25 seconds, the
bot is told it's waiting and can check later with `device_request_result`. Nothing that was asked or found is written to
the logs. Every bot needs its own copy of this version, and it works after Hermes restarts.

Version 0.9.20 tells a restart script **which chats typed in Hermes HQ or the desktop app are mid-reply**
(`GET /api/plugins/hermes-hq-push/serve/working`). Hermes keeps those turns only in memory, so a restart that waited for
`/sessions/working` (the gateway's work) to be empty could still cut off a reply you were watching. Nothing changes in
the app. It works after Hermes restarts.

Version 0.9.21 lets **Device Access share everything else on your phone**. Each kind is its own page under **Settings ›
Device Access** in Hermes HQ, everything starts off, and the phone checks every request against its own switches:

| Kind | A bot can | Tool |
|---|---|---|
| Location | ask where you are: city, street address or exact, as you choose; only while the app is open unless you allow While Locked | `device_location` |
| Reminders | see your reminders and lists; with Add, Edit & Complete, add, change and tick them off | `device_reminders` |
| Calendar | see your events; with Add Events, add new ones (never change or delete) | `device_calendar` |
| Alarms & Timers | set alarms and timers that ring like the Clock app's, and see or cancel only the ones it set | `device_alarms` |
| Photos | find photos by date, place, favourite or kind; with Send Photos, get a copy of one (saved on the computer for a day) | `device_photos` |
| Health | read summaries of the Health data you switch on: since 0.9.46 any kind the Health app has, before that steps, sleep, workouts and heart rate (only while the phone is unlocked) | `device_health` |
| Motion & Fitness | see whether you're walking, running, driving or still, and today's steps | `device_motion` |
| Music | see what's playing; with Play & Pause, play from your library, pause or skip | `device_music` |
| Email & Text Drafts | write an email or a text that waits on your phone as a draft: nothing is sent until you tap Send | `device_draft` |

A bot sees a kind's tool only while a phone shares that kind, and a request whose switch is off is answered without
waking the phone. A photo a bot fetches is kept in the plugin's data folder (`device-access-files`) for a day. Nothing
that was asked or found is written to the logs. An older Hermes HQ answers Contacts only and says the others aren't
available yet. Every bot needs its own copy of this version, and it works after Hermes restarts.

Version 0.9.22 keeps **a bot's Lock Screen card live while your phone is locked**. Hermes HQ shows a card on the Lock
Screen (and in the Dynamic Island) while a bot works on a long reply, naming the step it's on ("Running npm test…"),
the two before it and how many so far. iOS stops the app about 30 seconds after the phone locks, and from then on only
a push can change the card. Now the plugin pushes each new step the bot takes, at most one every 3 seconds (the last one
always arrives), and the card says Done when the turn ends. The steps are sealed with your phone's key, like alerts: the
relay and Apple see only that a card changed. A step a bot takes in a helper doesn't show, and a card whose turn you
stop from the app is left to the app. Every bot needs its own copy of this version for its steps to show (the card still
ends when the reply is written without it), and it works after Hermes restarts.

Version 0.9.23 makes **the Lock Screen card say Done when a long reply ends**. iOS lets an app send only so many urgent
updates to its cards; a long reply's steps went as urgent ones and could use them all up, so the push that ends the card
was held back and its clock kept counting until you opened the app. Steps now go as ordinary updates, which iOS doesn't
ration, and the end goes as an urgent one and again 30 seconds later as an ordinary one. Apple also keeps the end for a
while if your phone can't be reached at that moment. Every bot needs its own copy of this version, and it works after
Hermes restarts.

Version 0.9.24 lets **a quick reply's card go soon after it's done**. Hermes HQ now shows a card for any reply that's
still going when you leave the app, not only one that has already run 20 seconds. When a reply took under a minute, its
Done card leaves the Lock Screen a minute later instead of 15; its notification says the rest. The second copy of the end
now says the same end time as the first.

Version 0.9.25 adds the **Terminal**: a shell on your gateway's computer, typed into from Hermes HQ, with copy and
paste both ways. Open it from **More › Terminal** (you can make it a tab, or a place in the Mac's sidebar) or from
**Bot Info › Computer › Terminal**. The first time, Hermes HQ asks whether to turn it on for that computer, then asks
for Face ID or your passcode each time it opens. The shell is the login shell of the account Hermes runs as, started
in its home folder, like an SSH login: what you type there has the same access Hermes has. It keeps running for 15
minutes after you leave the page (or your phone locks), so you come back to the same shell and what it printed. To turn
the Terminal off on a computer, create an empty `terminal.off` file in `~/.hermes/plugin-data/hermes-hq-push/`. It
works on Mac, Linux and Windows, and only after Hermes restarts. On Windows the shell is PowerShell (PowerShell 7 when
it's installed); Hermes in WSL gets your Linux shell.

Version 0.9.26 mints the **Grok Live** voice session a short-lived xAI token. `GET .../voice/grok-live/status`
tells Hermes HQ whether a bot's xAI credentials (your subscription sign-in or `XAI_API_KEY`) can start a voice
session, without calling xAI; `POST .../voice/grok-live/session` asks xAI for a token good for a few minutes so the
app's voice session can talk to Grok directly — the bearer that authorizes it never reaches the phone. Configure a
bot's model, voice and extra instructions under `voice.grok_live` in its `config.yaml`.

Version 0.9.27: a bot with no Grok sign-in of its own uses the main bot's, so one `hermes auth add xai-oauth`
lets every bot on the computer talk with Grok Live, each with its own name, voice and instructions.

Version 0.9.35: Grok Live runs on your Grok subscription only. An `XAI_API_KEY` is never used for it, not even as a
fallback: a bot that has only a key borrows the main bot's Grok sign-in, and status says Subscription for every bot.

Version 0.9.28 keeps the names Hermes gives new chats short: 2 to 4 words, at most 28 characters, so a chat's name
fits on one line of the Sessions list on an iPhone instead of being cut off. Names you type yourself are never changed.

Version 0.9.32 adds **Tasks**: a shared to-do list between you and your bots. A task is yours (a bot only ever adds
a short note to it, never changes it), a bot's own, or split into steps for each of you. A bot can flag its own work
`prep` to get it ready ahead of time with its `tasks_next_prep` tool — research and drafts only, never sending,
booking, buying, paying, posting or deleting anything — and tells your phone when its part is `ready` or
`needs_you`, with the Approve/Deny relay this plugin already sends everything else through. A bot with open prep
work gets its own "Task prep" scheduled job every few hours (`prep_every_hours` in Tasks' settings), removed again
once it has none left. Quiet hours and a daily cap on prep runs are also in Tasks' settings. Never touches Hermes's
own Kanban, which stays a bot's separate, private to-do list.

Version 0.9.36: Tasks can be turned off entirely (`enabled: false` in Tasks' settings, or Settings › Tasks in the
app): the task tools disappear for every bot, the shared-tasks note and the "Task prep" jobs go away, and the list is
kept for when it's turned back on. It also adds your own sections: `GET`/`PUT /tasks/sections` keeps them in your
order, each task remembers its section and its place in it, and Tasks' settings get `group` (smart or sections) and
`sort` (smart, due or added). Bots can file a task into one of your sections by name with `tasks_add` and
`tasks_update`, but they can't make new sections.

Version 0.9.37 makes a task a workspace. A bot adding a task gives it a time ("tomorrow" alone means 9:00) and, when
the answer will arrive somewhere it can check (an email, a message), takes that part: a `watch` line says what it looks
out for, and its prep run comes an hour before the task is due (a one-shot "Task check" job), checks first, then says
what happened or gets your next move ready (a short draft you can send). A task can keep its own chat (`chat`:
`{profile, session_id}`): the app makes it hidden, so it never shows in the session list, and the bot is told which
task the chat belongs to on every turn.

Version 0.9.38 gives every task a **Task Page**: one page with everything about the task and where it stands, in the
same look for every task. The add-on draws it (`GET /tasks/{id}/page`) from the task itself (status, steps, what was
made, its history) and what its bot writes with `tasks_update(page={summary, next, blocks})`: where it stands, what's
next, and parts such as options compared in a table, the numbers that matter, or what it found. The bot writes words,
never HTML. The `task-page` skill (hermes-hq-push:task-page) tells bots what goes on it.

Goals, as installed with 0.9.73 (released as 0.9.68 and 0.9.72 from installed copies and never committed until this
reconcile; code comments still call them 0.9.65 and 0.9.66, the numbers they were built under). Your goals sit beside
your tasks (`goals.py`, `POST/PATCH/DELETE /goals` and in `GET /tasks`, list shape 7: `goals`, a task's `goal` and
`milestone`): a finish line, a day, a number, milestones and the tasks under them. Bots can help on a goal you give
them (list shape 8): one bot leads it and up to five help; the lead offers a plan and a short weekly check-in as cards
you tap (tools `goals_list`, `goals_offer`, `goals_update`, `goals_next_review`), keeps where the goal stands and its
Goal Page (`GET /goals/{id}/page`, skill `hermes-hq-push:goal-page`), and reads the goal's number only when you say it
may. A bot never ticks your task, a milestone or the goal itself, and the "Goal check-in" job is kept to research and
drafts.

Version 0.9.87 gets the cards used. A bot with many tools has Hermes's tool search on, which keeps add-on tools out of its tool list, so asked for hotels it wrote a table and never called `show_hotels`. A short note in app chats now tells the bot to show the flights, hotels and products it finds with `show_flights`, `show_hotels` and `show_products`, and how to reach one that is tucked away. It names only the card tools that exist on this computer, reaches chats that began before this version, and `show-cards.off` in the add-on's data folder turns it off.

Version 0.9.86 adds travel cards: `show_flights` and `show_hotels` draw the flights and hotels a bot finds as cards in the app (times, stops, fare and bag rules; photos, rating, nightly rate and cancellation), with Book and Watch Price on each. A booking still stops at an order card you tap Pay on. An email draft card carries Cc and Bcc, and the bot sends to exactly the people on the card. Each item on an order card can name its own product page and options, which the card opens (only on the shop's own site). A connector tool you set to ask first in the app's Connectors asks you before it runs, also when Hermes reviews approvals by itself. Versions 0.9.83 to 0.9.85 were not published on their own and are part of this one: a task a bot offers for a goal carries the notes, page, link or file it names; bots make pages from a starter in the app's look and check them at phone size (the `html-page` skill); a coding project's chats report the size of their unmerged changes and a waiting pull request's checks; bots name the background jobs they start; and a bot marks your task done when you tell it to in the chat.

Version 0.9.82 cuts the back-and-forth when a bot buys something for you. A bot asks once: the order card's rule now tells it to use the address, contact details and payment method you already gave it or have saved, go on to the final total, and ask about the items, the total, the address and the payment together on the card, never one at a time; a task that is the bot's alone gets the same rule for its one question. A task check that finds what it waited on has come through (a payment you approved, a confirmation, a reply), with a next step it may not take itself, no longer marks the task ready and ends without a word: it sets the task to needs you, says what came through and what is left, and offers "Finish it now" and "Hold off". A card fill a bot asks you to confirm (Hermes's `vault-payment` prompt) and an MCP server's question or trust prompt (`mcp-elicitation/<server>`, `mcp-trust/<server>`) now alert your phone as a command's approval does; they used to wait unseen until they timed out. The version the add-on reports (`pluginVersion`) is the manifest's again (it had stayed at 0.9.76).

Version 0.9.81 stops a 1Password login from filling on a neighbouring host (issue #11). Which hosts count as a login's site no longer rests on guessing a registrable domain from a short shared-hosting list, where any unlisted host (`alice.duckdns.org` and `mallory.duckdns.org`) read as one site. With a leading `www.` dropped, the page may be the saved host, a parent of it (its owner already holds the saved host's DNS) or a host under it; two sibling hosts never match, listed or not. The same rule picks the frame a login or code goes into. A login saved on one sibling (`app.example.com`) no longer fills on another (`login.example.com`); Hermes's exact-origin refusal applies there.

Version 0.9.76 puts the public pull request's number on merged work. A repository whose main is published elsewhere (the publisher's `hq-public-published` marker) is now read with its receipts file, `hq-public-receipts.jsonl` beside the marker (one `{"commit", "url"}` line per published private commit), so a chat's merged branch carries the number of the public pull request that carried it. Links made in the app before 0.9.76 read the receipts too. Work published before receipts were kept, or not published yet, stays merged with no number.

Version 0.9.75 tells the app about every pull request and branch a chat has, not just one. Each answer from `POST /sessions/pr-status` now carries `items` (newest first, at most 12): each pull request the chat opened (from GitHub) and each branch of its own (from git), with a pull request and the branch it was opened from joined into one. The one answer is decided more honestly: a pull request GitHub couldn't be asked about outranks an older merged one (it may be the one still open), a chat whose work landed isn't shown closed because a later attempt was given up, a closed pull request whose branch reached main reads as merged, and older work left out by the caps (now 12 pull requests) makes the answer unknown rather than finished. Your own Closed mark now also covers a pull request still open on GitHub (its item still says open). A request with `"fresh": true` for up to four chats asks GitHub again about their waiting pull requests instead of reading the three-minute cache, so a chat opened in the app shows a merge within seconds. Checked against 536 real chats: no chat's one answer changed.

Version 0.9.74 suggests titles for newly created tasks. Only the task title is sent to the auxiliary model; notes and sources stay out of the request. The original title stays until you accept a suggestion, and a late suggestion cannot overwrite an edit. Existing tasks are not queued by the update.

Version 0.9.73 stops a chat's new branch reading as merged before it has a commit (0.9.72 is the Goals release,
installed from its own branch). A branch made from main and not committed on yet is nothing at all; 0.9.71 said so
only while the commit it was made from stayed on main's own line of first parents, and a main moved up to another
branch's merge takes its older tips off that line. A branch's own log now decides: no move that made a commit, and
only ever at commits main itself was at (main's log of moves), is no work of its own, however main moved since.

Version 0.9.71 answers a coding project's marks quickly on a busy computer. Each question to git is a process, and
0.9.70 asked about six per chat every time (measured on a computer that was building: 10 seconds for 64 chats). What
git said is now kept for as long as it stays true: a repository's branches and their tips are read in one question
every 20 seconds, and nothing else is asked again until a branch or main moves (the same 64 chats: 0.04 seconds,
two questions). The answers are the same.

Version 0.9.70 lets you say two things from the app instead of a file. Mark as Closed on a chat in a coding project
puts the closed mark on it until you reopen it (`PUT /sessions/pr-mark`, kept in `session-pr-marks.json`): a label
only, nothing in any repository changes, and work that has since reached main shows as merged regardless. Repository…
on a project links it to a git repository on this computer (`GET`/`PUT`/`DELETE /sessions/pr-repos`, kept in
`session-pr-links.json`): the add-on asks git whether the folder is a repository, takes the repository a worktree
belongs to, picks main (the remote's when this checkout's own is behind it), notices a publisher's
`hq-public-published` marker, and otherwise reads the public repository from the `origin` remote. Linked
repositories are read together with any listed by hand. A branch that was deleted with its commits in no other
branch and not in main now shows as closed; the add-on remembers the tip of each branch it saw waiting
(`session-pr-tips.json`), so this works for chats whose history holds no commit id. Only the owner may link or
mark, and only their own chats. Work merged into main now shows merged at once; 0.9.69 showed an open mark until
main's public copy had it, which read as a mistake (the open mark is a pull request open for review on GitHub).

Version 0.9.69 makes those marks follow your own repositories, for the common case where no chat opens a pull
request by hand: a chat works on a branch, the branch is merged into main, and main is published elsewhere. List the
repositories in `plugin-data/hermes-hq-push/session-pr.json`:

    "repos": [{"path": "/path/to/checkout", "main": "main",
               "published_marker": "hq-public-published", "public_repo": "owner/repo"}]

A chat then shows a draft mark while a branch it made has commits that are not in main, an open mark once that work
is in main but main's public copy doesn't have it yet (`published_marker`: a file in the repository's git folder
holding the last published commit; leave it out when main itself is the public branch), and merged after that. A
branch counts as a chat's own only when the chat, or a subagent it started, made it or committed on it; nothing in the
history is run, and git is asked read-only, inside the listed repositories only. A pull request the chat opened itself
with `gh pr create` still shows as GitHub has it, and comes first while it waits. A chat that made a branch none of
the listed repositories knows shows unknown, not "no pull request". Running a script that publishes other chats'
merged work no longer makes its pull requests the chat's own (0.9.67 did). (0.9.68, Goals, is installed from another
branch and not published here yet.)

Version 0.9.67 lets Hermes HQ show what became of the pull requests a chat opened: none yet, draft, open, merged
or closed. A pull request counts as a chat's own only when that chat (or a subagent it started) opened it and the
result of that very command printed the link: `gh pr create`, or a publisher script (`public-prs.py`, not
`--dry-run`) printing its `<commit>  <link>  <title>` line. A link the bot only read, reviewed, listed or wrote in a
message never counts, and nothing in the history is run or opened. The add-on first checks that the signed-in person
owns the chat, on the same read-only snapshot of the session store it then reads. A chat with a draft or open pull
request shows that one even when an older one of its has merged.

The computer's own GitHub sign-in (`gh`, `GH_TOKEN`) is never used, and GitHub is asked about a pull request only
when you allow it in `plugin-data/hermes-hq-push`:

- `session-pr.json` with `"public_repos": ["owner/repo"]` declares repositories public. GitHub is then asked
  without any sign-in, by pull request number. A merged answer is final and is remembered in
  `session-pr-merged.json`, so it is asked once.
- `session-pr-github/<sha256 of your sign-in subject>.token` holds a GitHub token of your own (mode 0600). GitHub
  is then asked with that token, for your own chats only.

A pull request in any other repository shows unknown, and so does a chat whose history can't be read. When GitHub
doesn't answer (offline, or its hourly limit for requests without a sign-in), the last answer stays. A chat that
opened no pull request shows none.
The status reports `sessionPRStatus: true` only on a Hermes runtime that has every session ownership API the
endpoint uses; elsewhere the endpoint answers 501. The change takes effect when the serving process next loads
the updated plugin, and the companion app must support the status display.

Version 0.9.66 keeps your other devices quiet about a reply you are already reading. The app on the device that has a chat in front of you tells the add-on so (the chat's id and the bot's name, nothing else), again every 20 seconds while that stays true; a reply written in that chat then alerts no device. Each report runs out after 45 seconds, so a device that sleeps or loses its link stops counting by itself, and a Mac nobody has touched for two minutes stops reporting. Approvals, questions and tasks still alert everywhere, because a bot is waiting on them. An app older than this change reports nothing and is alerted as before.

Version 0.9.64 shows you the picture before the question. A bot that made a mock-up or screenshot for a choice and looked at it itself, then asked you to pick, used to put up the question card without ever posting the picture (the card can't show one), so you chose between pictures you hadn't seen. Now that question is held once, and the bot is told to post the picture (its `MEDIA:` lines) first and ask again. A picture you sent, an old file or a web image doesn't count, and a second try in the same turn always goes through (show_first.py).

Version 0.9.63 adds **ask-to-act cards**. Before a bot does something for you (answers someone, replies in a thread, books, files or changes something) it asks with `ask_to_act`, and the app draws one card: who asked and their words, a link to the conversation, a button per choice, and the reply it would send for each choice with Send and Edit. Nothing is sent until you tap Send. It works when a scheduled job's report reaches the bot's chat too (asks.py).

Version 0.9.62 reaches a **sign-in inside a page**. Many sites show their sign-in as a frame from another of their
addresses inside the page (DoorDash's comes from identity.doordash.com inside www.doordash.com), and Hermes's vault
only read whole tabs: asked for that frame's code it said no tab was open, and asked for the page's it found no code
box. Now a login or code the bot names goes into the frame of an open tab on that site, or, when the bot names the page,
into a frame of the same site that holds the form (never another site's, and never shared hosting such as
`*.github.io`). The card on your phone names the frame's own address, a saved login still fills only on the address it
was saved for, and the fill re-checks that address inside the frame just before it writes. The frame is used for that
one call only. Asking for a code also sends the bot to look in your mail once per site, however it names the site, and
a "no code field" answer now tells the bot you can't type into its browser from the app, so it hands you the browser
instead of asking you to type into a page you can't see.

Version 0.9.61 gives Dashboard tiles a new default look, Soft: a calm card in your own theme's colours, the heading
in plain words, the numbers on small cards of their own, and a faint light in the corner only when a tile warns or is
in trouble. A tile is drawn in it unless you ask a bot for another look (`app`, `midnight`, `terminal`, `vivid`).
Tiles made before keep the look they have until they are changed or remade. (0.9.59 and 0.9.60 were not published.)

Version 0.9.60 adds **shopping cards**. Asked to find, compare or recommend something to buy, a bot shows the products
it found on store pages as product cards in the Hermes HQ app (its `show_products` tool): photo, name, price (the old
price struck through), store and rating, and a tap opens the product's page with the bot's reasons, other stores'
prices, **Buy** and **Notify Me**. Buy sends the bot a message: it takes the item to the last step of checkout in its
browser and shows the order as a card (`order_card`) with the store, items, charges, total, **Cancel** and **Pay**.
From the moment the bot shows an order card until you tap Pay or Cancel on it, the bot cannot do anything in its
browser in that chat: no clicking, typing, navigating or filling a card (the add-on refuses those tools; tools that
only read the page, such as a screenshot, still work). A yes typed in words doesn't lift it; only the card's buttons
do. After Pay it may place that order; after Cancel it is free again. The waiting orders are kept in
`shopping-orders.json` in the add-on's data folder, so a restart doesn't lift the block, and a card nobody answers
for a day expires. The add-on itself pays for nothing; it checks the cards (https links, prices as numbers, an old
price above the price, the counts) and tells the bot in words what to fix. Notify Me asks the bot for a scheduled job that tells you when the item is back in
stock or cheaper. Bots get the two tools only once an app that draws the cards has connected to this computer
(`PUT /shopping/cards`; `shoppingCards`, `shoppingCardsShown` and `shoppingPayBlock` in the status), and only in the
app's own chats; create `shopping.off` in the add-on's data folder to turn them, and the block, off for every bot.

Version 0.9.59 adds Allow in 1Password. Hermes unlocks 1Password with your master password and asks again after half
an hour idle. Now the app's unlock sheet and Passwords page have an Allow in 1Password button: the 1Password app on
Hermes's computer asks you to allow it (a click or Touch ID, no master password), you pick which vaults your bots may
fill from, and the add-on makes a read-only key for those vaults with 1Password's own command-line tool
(`op service-account create … --vault <id>:read_items`) and saves it in each bot's `.env` as
`OP_SERVICE_ACCOUNT_TOKEN`, the name Hermes already reads. From then on 1Password is never locked for your bots.
1Password never lets such a key read your Private vault, so move a login into one of the vaults you picked (the app can
make one named Hermes). The key never leaves the computer and is not sent to the app; a bot that already has a key of
its own keeps it; Disconnect in the app removes the key from the bots (cancel the key itself at 1Password.com ›
Developer › Service Accounts). Needs 1Password 8 with Settings › Developer › Integrate with 1Password CLI turned on,
and the `op` tool installed, on the computer running Hermes.

0.9.59 also alerts your phone when a bot waits for a login, a one-time code or a password manager's unlock in a
chat you don't have open. Hermes told plugins about approvals, questions and sudo passwords only, so those three
waited unseen.


Version 0.9.58 ties a plugin update you approve in the app to what the app showed you (the last open finding of the
outside review). Hermes fetches the new version into a copy before it asks anything, and lists what that copy needs
above each question. The add-on now says yes only when that list is exactly what you saw: the Python packages, the
security scan's findings (severity, kind, file and line) when the scan said "caution", and what a catalog plugin's new
pin adds. If the plugin changed in between, by even one package or finding, it says no, nothing changes, and the app
is told what Hermes asks about now. `--force` is no longer passed: a "caution" scan is accepted at Hermes's own
question, on its findings. The app tells it what it showed (`packages`, `findings`, `capabilities` on
`POST /plugins/update`; `pluginUpdatesChecked` in the status); an older app's yes, which carried no list, is now
answered no. Pinning the update to one exact revision needs `hermes plugins update` to take one, which Hermes doesn't
yet. Also fixed: an update whose new version declares new capabilities no longer waits five minutes at Hermes's
"Grant these capabilities?" (its console drops the `[y/N]`, so the add-on didn't see a question) and then reports a
failure for an update that went in; the question is answered no, as before.

Version 0.9.57 lets a bot that is handling a scheduled job's report ask you something. That turn runs in the
messaging gateway with nobody watching the chat, so Hermes refused the bot's question (`clarify`) and a command's
approval waited unseen until it timed out. Both now show as their usual card in the app's copy of the bot's chat, the
phone is alerted, and your answer goes back to the waiting turn. When the app can't be reached, the bot is told to
put its question in its reply instead. Turns you typed, other apps' chats and hosted rooms are left as they were.
Both the gateway and `hermes serve` need the new version (restart them after updating).

Version 0.9.56 closes two findings from an outside review. The Task prep, Task check and Task ideas jobs are now the
add-on's by the id it recorded when it made them, not by their name: a job of yours that happens to be called
"Task prep" is never edited, run or removed. A job from an older version is taken over only when its name, its prompt
and its delivery all match what the add-on writes. And the tile check (`check_tile.py`) now draws a tile in a browser
that can read no other file and reach no network: no `--allow-file-access-from-files`, every host name unresolvable,
a dead proxy, and the sandbox left on unless the browser can't start with it.

Version 0.9.55 gives Tasks lists a keeper. A list (`GET`/`PUT /tasks/sections`) now also carries `color`, `icon`, `bot`
(the bot that keeps it), `adds` (`ask`: that bot offers new tasks first) and `today` (`false`: left out of Today);
a field an older app leaves out is kept. Bots get `tasks_offer`: a list to make, a batch of tasks for a list, or
tasks to move into one, shown in the app as one card. Nothing changes until you tap (`POST /tasks/offers/{id}` with
`accept`, `decline`, `move`, `leave` or `undo`); a list you declined isn't offered again, and the bot whose list you
accept becomes its keeper. `tasks_add` and `tasks_update` take `source` (`app`, `who`, `at`, `ref`, `link`): the
same `ref` never becomes a second task. A finished task refuses anything more from a bot. The bot that added a task
for you may fill it in until you change it; on your own task a bot can file, tag, note, or offer to close it
(`offer_done`, answered in the app with Mark Done or Keep Open), and is told so once instead of refused each time.
`GET /tasks` gains `lists: true` and `offers`.

Version 0.9.50 remakes how Dashboard tiles are made. A bot no longer writes a tile's HTML and CSS: it gives the
tile's data as a small JSON spec (a headline, then blocks: a chart, status boxes, rows, meters, stats) and
`make_tile.py` draws it with a designed kit, in one of four looks (`app`, the person's own theme; `midnight`;
`terminal`; `vivid`). The kit lays the tile out for every size and fits it by itself: what doesn't fit goes in order,
a list shows what fits and says `+6 more`, what is wrong comes first. `make_tile.py set` gives a tile new data on a
schedule and `fail` keeps the last good data and says why. `check_tile.py` draws the page in the windows the app
really uses (a card is 390 wide) and finds scripts that fail, parts cut off or drawn over each other, small text,
crowded and half-empty tiles; it tries the next browser when one won't start and says `NOT DRAWN` plainly when there
is none. `custom.html` is the hand-written page for what the kit can't draw.

Version 0.9.52 lets the app finish a plugin update that adds Python packages. Hermes refuses such an update from an app ("run `hermes plugins update <name>` in a terminal to review them"). The app now shows you the packages and asks; when you say yes, `POST /plugins/update` runs Hermes's own `hermes [-p <profile>] plugins update <name>` on this computer, on a terminal of its own, and answers that one question yes. A plugin that got wider is accepted only if you accepted it in the app; any other question is answered no. `GET /plugins/update/<job>` says how it went. Mac and Linux only (`pluginUpdates` in the status says whether this computer can).

Version 0.9.51 stops the alert for a reply a bot writes to another bot. When another bot's message starts the turn ("Message from 🤖 otto (@otto): …", or a queued turn Hermes recorded a bot as the author of) and that bot wrote
first, the answer goes back to that bot and the app folds it into that bot's line, so the alert opened a chat with
nothing new in it. A reply that opens by speaking to the other bot by name ("Thanks, Otto. …") is quiet too. A
reply after a bot answers what this bot asked it still alerts: that is the news for you.

Version 0.9.49 sends no alert for a reply that is only a silence marker: a bot answering another bot's message often
ends its turn with a bare `NO_REPLY`, and the phone showed it as an alert. The add-on uses Hermes's own list (`NO_REPLY`,
`SILENT`, `[SILENT]` and the rest), and a reply that closes with a `NO_REPLY` line alerts without that line. Version
0.9.48 brings the task page's changes (in a task's own chat your word counts, one-tap answers) on top of drafts.

Version 0.9.47 adds **drafts for review**: when a bot has written something that would go out in your name (a Slack
message, an email, a text, a post), it shows it in the Hermes HQ app as a card with the text, **Send**, **Edit** and
**Don't Send** (its `draft_message` tool) instead of sending it or pasting it in the reply. Nothing goes out until you
tap Send; your edits are what gets sent. The add-on sends nothing itself: your answer goes back to the bot as a
message, and the bot sends the final text with whatever it already uses for that place. While a draft waits, or after
Don't Send, the add-on refuses a sending tool that carries that draft's text (best effort: it can't see a send hidden
in a shell command). Bots get the tool only once an app that draws the card has connected to this computer; create
`drafts.off` in the add-on's data folder to turn it off for every bot.

Version 0.9.46 lets a bot read **any kind of Health data you switch on**, not only steps, sleep, workouts and heart
rate. In Hermes HQ, **Settings › Device Access › Health** lists the Health app's groups (Activity, Heart, Nutrition,
Symptoms and the rest); each has a switch per kind and Turn On All. `device_health` with action `types` lists what's
on, without waking the phone, and a summary names types by their HealthKit identifier or asks for whole groups. An
older Hermes HQ still shares only the four it had. Every bot needs its own copy of this version, and it works after
Hermes restarts.

This plugin was called `dispatch-push` before version 0.5. It takes over the old plugin's registered phones on its
first start, and reads the old `DISPATCH_*` settings when the new names aren't set.

### Install

The easy way: open Hermes HQ. When your gateway doesn't have this plugin, or still has `dispatch-push`, Hermes HQ
offers to have your Hermes bot install it. Tap **Ask**, and the bot installs it, removes the old one, restarts Hermes,
and Hermes HQ turns notifications on.

To do it yourself, on the computer that runs your Hermes gateway:

```bash
hermes plugins install mrcharlesiv/hermes-hq-plugins/hermes-hq-push --enable
hermes plugins remove dispatch-push   # only if you had the old plugin
```

Then restart `hermes serve` or `hermes dashboard` (or the Hermes app). The plugin's routes appear only after a restart.
If a messaging gateway runs too (for Telegram, Discord and other apps), restart it as well: approvals in those apps
alert only once it has loaded the plugin. Open Hermes HQ and make sure **Settings › Notifications** is on. Hermes HQ
registers for push the next time it opens.

To update an older copy, run the same install command with `--force` added, then restart the same way. From 0.9.1, also
run `hermes gateway restart`, or Delete keeps failing. Your registered
devices are kept. If the old `dispatch-push` is still installed beside it, only `hermes-hq-push` sends alerts, so you
never get two.

### What it sends, and who can read it

Your gateway can't send to Apple itself: only the app's developer holds Hermes HQ's Apple push key. Alerts go through
the Hermes HQ push relay (`https://push.getdispatchapp.com`), which holds that key and forwards them to Apple.

**The relay can't read your alerts.** When Hermes HQ registers, it gives your gateway a key that only your phone holds
(AES-256). The plugin seals every alert with it before it leaves your computer. The relay and Apple carry only
"Hermes HQ: New notification" and a sealed box, and your phone opens the box. The relay learns your phone's push token,
when an alert was sent, and whether it offers Approve/Deny. It never learns who it's from or what it says. It refuses
anything that isn't sealed, so nobody can use it to put other words on your lock screen.

Approve and Deny go from your phone straight to your gateway, with your normal sign-in, never through the relay.
Each one can only answer the request its alert was sent for.

In Hermes HQ, **Settings › Notifications › Show Previews** off makes alerts say only who needs you, even inside the seal.

### Settings

All optional, in `~/.hermes/.env` (the old `DISPATCH_PUSH_RELAY_URL` and `DISPATCH_PUSH_RELAY_KEY` still work):

| Variable | What it does |
|---|---|
| `HERMES_HQ_PUSH_RELAY_URL` | Another relay's HTTPS address, or `off` to send nothing |
| `HERMES_HQ_PUSH_RELAY_KEY` | A key, for a private relay that asks for one |

### Limits

- Chats in Telegram, Discord and other apps alert only with version 0.9 or newer, and only once Outside Chats is on in
  Hermes HQ. A question a bot asks in its reply alerts; one it asks with its clarify tool doesn't.
- Approvals time out after 60 seconds by default. To answer from the lock screen, raise `approvals.timeout` in
  Hermes' `config.yaml` (300 works well).
- Approvals from the lock screen need Hermes' default, unisolated turns (`dashboard.turn_isolation` off).

### Tests

```bash
python3 -m unittest hermes-hq-push/test_push.py
```

`test_input_alerts.py` needs only Python: `python3 -m unittest test_input_alerts.py` from the plugin folder.
`test_live_steps.py` (Lock Screen steps: the app's step words, the routes, pacing and the sealed card) runs like the
others: `~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_live_steps.py`.
`test_own_chats.py`, `test_desktop_opens.py`, `test_cron_working.py` and `test_gateway_logins.py` run against a Hermes checkout, with its own python, from the
plugin folder: `~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_cron_working.py`.

`test_terminal.py` (the Terminal's shells, replay, takeover and limits) opens real shells on this computer, through
the Mac and Linux runner and through the Windows one on a stand-in for Windows' pseudo-console:
`~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_terminal.py`.

`test_tasks.py` (the Tasks store, its permission rules, the prep job and the routes) covers the store and tools with
plain Python, but the "Task prep" job tests drive Hermes's own `cron.jobs` and the route tests need `fastapi`, so run
the whole file the same way: `~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_tasks.py`.

Tests that need `cryptography` or `fastapi` skip without them.

## hermes-hq-browser: watch and take over a bot's browser

See the browser a bot is using, live, in Hermes HQ, and take it over when it needs you: to sign in, enter a code or
solve a CAPTCHA. Bots keep every browser tool Hermes gives them; the plugin overrides nothing. It adds one tool,
`browser_ask_user`, which lets a bot hand you its browser and wait until you give it back.

### Install

The easy way: open **Watch Browser** for a bot in Hermes HQ. When your gateway doesn't have this plugin, Hermes HQ offers
to have your Hermes bot install it.

To do it yourself, on the computer that runs your Hermes gateway. Each bot profile loads its own plugins, so install
it for the main profile and for each other profile (`hermes profile list`):

```bash
hermes plugins install mrcharlesiv/hermes-hq-plugins/hermes-hq-browser --enable
hermes -p <profile> plugins install mrcharlesiv/hermes-hq-plugins/hermes-hq-browser --enable
```

Then restart `hermes serve` or `hermes dashboard` (or the Hermes app) and the messaging gateway, if one runs. To update
an older copy, run the same commands with `--force` added. This plugin was called `dispatch-browser`
before; remove that one first (`hermes plugins remove dispatch-browser`), then check that `browser` is still in any
`platform_toolsets` list in `config.yaml` that had it (removing a plugin with tools can take its toolset out).

### Which browsers it shows

Hermes's own browser for each bot (its Chromium, or the Browser Use harness driving it), a Chrome you connected with
`/browser connect` (`browser.cdp_url` on this computer), and the real-profile Chrome. Not cloud browsers, Lightpanda,
or a remote debugging address.

Hermes closes its own headless browser when the bot's turn ends. A bot connected to a Chrome of its own keeps that
browser, and its sign-ins, between turns. Hermes HQ lists it whenever it's running, so you can open it, take it over
and sign in to sites before the bot needs them.

### How it works

The bot's browser is never exposed: the plugin reads Chrome's own screencast on this computer and relays it to Hermes HQ
through your gateway's normal sign-in. While you hold a browser, that bot's browser tools are refused (it can call
`browser_ask_user` to wait for you), and what you tap and type reaches only that browser's page.

## License

MIT
