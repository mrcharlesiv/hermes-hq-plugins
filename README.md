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
`test_own_chats.py`, `test_desktop_opens.py` and `test_cron_working.py` run against a Hermes checkout, with its own python, from the
plugin folder: `~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_cron_working.py`.

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
