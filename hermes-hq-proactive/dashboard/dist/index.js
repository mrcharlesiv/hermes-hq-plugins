/* hermes-hq-proactive (Beta): the plugin's page.
 * Uses window.__HERMES_PLUGIN_SDK__ so the same file runs in Hermes Desktop and in the
 * Hermes HQ app (iPhone and Mac) with no build step.
 */
(function () {
  "use strict";

  const PLUGIN_NAME = "hermes-hq-proactive";
  const API = "/api/plugins/hermes-hq-proactive";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useMemo, useToast } = SDK.hooks;
  const {
    Card,
    CardHeader,
    CardTitle,
    CardDescription,
    CardContent,
    Badge,
    Button,
    Checkbox,
    Input,
    Label,
    Select,
    SelectOption,
    Toast,
  } = SDK.components;
  const { isoTimeAgo } = SDK.utils;

  // The words the page uses. One name per thing, everywhere.
  const CATEGORY_LABELS = {
    next_step: "Next steps",
    follow_up: "Follow-ups",
    pre_event: "Getting ready for something",
    prepared_draft: "Things coming due",
    pattern_automation: "Requests you repeat",
    stale_check: "Things gone quiet",
  };

  const MODE_LABELS = {
    observe: "Keep track only",
    prep: "Keep track and speak up",
    full: "Speak up, overdue ones first",
  };

  const KIND_LABELS = {
    waiting_on: "Waiting on someone",
    follow_up: "To follow up",
    pre_event: "To get ready for",
    commitment: "Said I would",
  };

  const STATUS_LABELS = { open: "Open", snoozed: "Snoozed", resolved: "Done", dropped: "Dropped", all: "All" };

  const CLOSED_BY = {
    user: "you",
    bot: "the bot",
    scan: "a check, which saw in your chats that it was done",
    task: "you, by adding it to Tasks",
    idea_dismissed: "you, by dismissing it in Tasks › Ideas",
    offer: "you, by marking its suggestion done",
    not_wanted: "you, by telling the bot you did not want it brought up",
  };

  // Where a find goes. The value is the setting's; the words are the choice as the page offers it.
  const PLACE_LABELS = {
    chat: "The bot messages me in its chat",
    ideas: "Make it a card in Tasks › Ideas",
    page: "Keep it on this page",
  };

  const WATCH_KINDS = {
    deadline: { label: "A date", target: "The date", hint: "2026-03-01, tomorrow, or by Friday" },
    file_change: { label: "A file changing", target: "The file's path", hint: "/Users/me/notes/release.md" },
    custom_timer: { label: "A regular look", target: "What to look at (optional)", hint: "The weekly numbers" },
  };

  const ACTIVITY_LABELS = {
    loop_captured: "Noted",
    loop_closed_by_scan: "Closed",
    offer_created: "Found",
    said_in_chat: "Messaged",
    offer_answered: "Answered",
    offer_closed: "Closed",
    offer_suppressed: "Held back",
    offer_blocked_safety: "Refused",
    fence_blocked_tool: "Stopped",
    settings_updated: "Settings",
    calibration_reset: "Reset",
    sweep_completed: "Check",
    sweep_quiet: "Check",
    deck_offered: "Task deck",
    deck_task_started: "Started",
    deck_task_hidden: "Hidden",
    scout_started: "Check-in",
  };

  const NUMBER_FIELDS = [
    ["daily_message_budget", "Most messages a day", 1, 10],
    ["daily_offer_budget", "Most finds a day", 0, 30],
    ["daily_interrupt_budget", "Most Time-sensitive ones a day", 0, 10],
    ["brief_hour", "Morning brief hour (0–23)", 0, 23],
    ["stale_waiting_days", "Days with no news before a nudge", 1, 30],
    ["scan_lookback_hours", "Hours of chats to read the first time", 1, 336],
    ["max_context_loops", "Open items mentioned per message", 0, 8],
  ];

  const SWITCHES = [
    ["loop_radar_enabled", "Keep track of what is left open", "Things you say you will do, things you are waiting on, and things the bot says it will come back to."],
    ["scan_recent_chats", "Read your recent chats", "Only your own one-to-one chats with this bot. Never group chats, scheduled jobs or helper agents."],
    ["idle_sweep_enabled", "Check every hour", "Looks at what is due, overdue or has gone quiet. Free when nothing is new."],
    ["allow_llm_in_sweep", "Let the check use a model", "A small model reads new chat messages and writes short drafts. Off: fixed rules only, at no cost."],
    ["chat_lookup", "Look into it before messaging me", "The bot looks the thing up first, reading only, and tells you what it found. Off: it only tells you what is open."],
    ["daily_brief_enabled", "Morning check-in in the bot's chat", "Posts a Chief-of-Staff check-in and ready-to-start task deck once a day. Nothing is posted when nothing is timely."],
    ["scout_tools_enabled", "Read connected tools during check-ins", "Reads your bot's connected calendar, Notion, email, Slack, and memory (strictly read-only) so every task card cites real dates and docs."],
    ["in_turn_enabled", "Go one step further in chats", "Asks the bot to do the one obvious read-only check and tell you, instead of only suggesting it."],
    ["pattern_detection_enabled", "Notice requests you repeat", "After the same multi-step request three times, suggests making it a skill or a scheduled job."],
  ];

  // Where finds go right now, in the few words a stat tile has room for.
  function whereWords(inbox) {
    if (inbox && inbox.to_chat) return "The bot messages you";
    if (inbox && inbox.to_ideas) return "Shown in Tasks › Ideas";
    return "Shown on this page";
  }

  // What the 24-hour tile counts: messages when finds are said in the chat, finds otherwise.
  function usageFor(st) {
    const b = st.budgets || {};
    if (st.inbox && st.inbox.to_chat && b.messages_limit_24h !== undefined) {
      return { label: "Messages in 24 hours", used: b.messages_used_24h, limit: b.messages_limit_24h };
    }
    return { label: "Found in 24 hours", used: b.offers_used_24h, limit: b.offers_limit_24h };
  }

  function ago(iso) {
    if (!iso) return "never";
    try {
      return isoTimeAgo ? isoTimeAgo(iso) : iso.slice(0, 16).replace("T", " ");
    } catch (_err) {
      return iso.slice(0, 10);
    }
  }

  // A date as the person reads it, on their own clock.
  // "2026-03-04" as "Wed, Mar 4". Read as a day on the calendar, not as a moment in UTC.
  function dayWords(day) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ""));
    if (!m) return "";
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    const words = { weekday: "short", month: "short", day: "numeric" };
    if (d.getFullYear() !== new Date().getFullYear()) words.year = "numeric";
    return d.toLocaleDateString(undefined, words);
  }

  function when(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
      return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    } catch (_err) {
      return iso.slice(0, 10);
    }
  }

  // Resolves true only when the text really reached the clipboard.
  function copyText(text) {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).then(
          function () { return true; },
          function () { return false; }
        );
      }
    } catch (_err) {
      /* fall through */
    }
    return Promise.resolve(false);
  }

  function briefJobLine(st) {
    if (!st.settings.daily_brief_enabled) return "The morning brief is off.";
    const hour = String(st.settings.brief_hour) + ":00";
    if (st.brief_job === "on") return "The morning brief is posted in this bot's chat at " + hour + ".";
    if (st.brief_job === "unavailable") {
      return "The morning brief could not be scheduled on this Hermes, so it is only shown here.";
    }
    return "The morning brief will be posted in this bot's chat at " + hour + ".";
  }

  function checkLine(result) {
    if (!result) return "Checked.";
    if (result.ran === false) return result.reason || "The check did not run.";
    return result.summary || "Checked.";
  }

  function ToggleSwitch({ checked, disabled, label, onToggle }) {
    return h(
      "button",
      {
        type: "button",
        role: "switch",
        "aria-checked": !!checked,
        disabled: !!disabled,
        className: "hq-pro-switch",
        onClick: onToggle,
      },
      h(
        "span",
        { className: "hq-pro-track" + (checked ? " is-on" : "") },
        h("span", { className: "hq-pro-thumb" })
      ),
      h("span", null, label || (checked ? "On" : "Off"))
    );
  }

  function OffExplainer({ saving, onEnable }) {
    return h(
      Card,
      null,
      h(
        CardHeader,
        null,
        h(CardTitle, null, "Proactivity is off"),
        h(
          CardDescription,
          null,
          "While it is off, nothing is added to your chats, the bot has none of these tools, and no checks run."
        )
      ),
      h(
        CardContent,
        { className: "hq-pro-off-card" },
        h(
          "div",
          { className: "hq-pro-tier-list" },
          h(
            "div",
            { className: "hq-pro-tier-box" },
            h("strong", null, "Keeps track"),
            h("p", null, "Notes what is left open in your own chats with this bot: what you said you would do, what you are waiting on, and what the bot said it would come back to. It closes them when the chat shows they got done.")
          ),
          h(
            "div",
            { className: "hq-pro-tier-box" },
            h("strong", null, "Suggests"),
            h("p", null, "When something is due or has gone quiet, the bot looks into it and messages you in its chat with what it found. A short brief is posted there each morning.")
          ),
          h(
            "div",
            { className: "hq-pro-tier-box" },
            h("strong", null, "Only reads and suggests"),
            h("p", null, "Nothing is sent, booked, paid, posted or deleted without you. What you keep turning down is suggested less.")
          )
        ),
        h(
          "div",
          { className: "hq-pro-actions" },
          h(Button, { disabled: saving, onClick: onEnable }, saving ? "Turning on…" : "Turn On Proactivity (Beta)")
        )
      )
    );
  }

  function SuggestionCard({ offer, busy, onRespond, onCopied }) {
    const inIdeas = offer.delivered_to === "ideas";
    const inChat = offer.delivered_to === "chat";
    const said = inChat && !!offer.said_at;
    const elsewhere = inIdeas || inChat;   // acted on somewhere else, so this page only reports it
    return h(
      "div",
      { className: "hq-pro-item" },
      h(
        "div",
        { className: "hq-pro-item-head" },
        h("span", { className: "hq-pro-item-title" }, offer.title),
        h(
          "div",
          { className: "hq-pro-meta" },
          inIdeas ? h(Badge, { tone: "default" }, "In Tasks › Ideas") : null,
          inChat ? h(Badge, { tone: "default" }, said ? "Said in the bot's chat" : "Next message") : null,
          offer.tier >= 2 ? h(Badge, { tone: "warning" }, "Time-sensitive") : null,
          offer.tier === 0 ? h(Badge, { tone: "secondary" }, "Kept for the brief") : null,
          h(Badge, { tone: "secondary" }, CATEGORY_LABELS[offer.category] || offer.category),
          h("span", null, ago(offer.created_at))
        )
      ),
      h("p", { className: "hq-pro-text" }, offer.summary),
      offer.why ? h("div", { className: "hq-pro-meta" }, "Why now: " + offer.why) : null,
      offer.draft_content ? h("pre", { className: "hq-pro-draft" }, offer.draft_content) : null,
      offer.suggested_action ? h("div", { className: "hq-pro-meta" }, "Next: " + offer.suggested_action) : null,
      inIdeas
        ? h(
            "div",
            { className: "hq-pro-note" },
            "Open Tasks › Ideas in the app to act on this. Add puts it on your list; Add & Start has the bot do it."
          )
        : said
          ? h(
              "div",
              { className: "hq-pro-note" },
              "The bot messaged you about this " + ago(offer.said_at) + ". Saying whether it helped makes this kind come up more or less often."
            )
          : inChat
            ? h("div", { className: "hq-pro-note" }, "The bot will look into this and message you at the next check.")
            : h("div", { className: "hq-pro-note" }, "Marking it done only records your answer. Nothing is sent or changed."),
      h(
        "div",
        { className: "hq-pro-actions" },
        said
          ? h(Button, { disabled: busy, onClick: function () { onRespond(offer.id, "accept"); } }, "It Helped")
          : null,
        elsewhere
          ? null
          : h(Button, { disabled: busy, onClick: function () { onRespond(offer.id, "accept"); } }, "Mark Done"),
        !elsewhere && offer.draft_content
          ? h(
              Button,
              {
                outlined: true,
                disabled: busy,
                onClick: function () {
                  // The answer is recorded only once the draft is really on the clipboard.
                  copyText(offer.draft_content).then(function (copied) {
                    onCopied(copied);
                    if (copied) onRespond(offer.id, "edited_accept");
                  });
                },
              },
              "Copy Draft & Mark Done"
            )
          : null,
        elsewhere
          ? null
          : h(
              Button,
              { outlined: true, disabled: busy, onClick: function () { onRespond(offer.id, "snooze", 24); } },
              "Snooze a Day"
            ),
        h(
          Button,
          { ghost: true, destructive: true, disabled: busy, onClick: function () { onRespond(offer.id, "dismiss"); } },
          said ? "Not Helpful" : "Dismiss"
        )
      )
    );
  }

  function TodayTab({ overview, busy, checking, onRespond, onCheck, onScout, onCopied }) {
    const brief = overview.brief;
    const st = overview.status;
    const pending = overview.pending_offers || [];
    const lastCheck = st.last_check;

    return h(
      "div",
      { className: "hq-pro-section" },
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(
            "div",
            { className: "hq-pro-item-head" },
            h(
              "div",
              null,
              h(CardTitle, null, brief && dayWords(brief.brief_date) ? "Today's brief · " + dayWords(brief.brief_date) : "Today's brief"),
              h(CardDescription, null, briefJobLine(st))
            ),
            h(
              "div",
              { className: "hq-pro-actions" },
              h(Button, { disabled: busy || checking, onClick: onScout }, "Run Check-In"),
              h(Button, { outlined: true, disabled: busy || checking, onClick: onCheck }, checking ? "Checking…" : "Check Now")
            )
          )
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          brief
            ? h(
                "div",
                { className: "hq-pro-section" },
                h("p", { className: "hq-pro-text" }, brief.summary),
                (brief.highlights || []).map(function (item, idx) {
                  return h(
                    "div",
                    { key: item.id || idx, className: "hq-pro-row" },
                    h(
                      "div",
                      null,
                      h("strong", { className: "hq-pro-strong" }, item.title),
                      h(
                        "div",
                        { className: "hq-pro-meta" },
                        (item.kind === "offer" ? "Found" : "Open") + (item.note ? " · " + item.note : "") +
                          (item.where === "ideas" ? " · in Tasks › Ideas" : item.where === "chat" ? " · in the bot's chat" : "")
                      )
                    )
                  );
                })
              )
            : h("p", { className: "hq-pro-empty" }, "No brief yet. It is written on the first check."),
          h(
            "div",
            { className: "hq-pro-note" },
            checking
              ? "Checking now. This can take a minute; you can leave this page."
              : lastCheck && lastCheck.summary
                ? "Last check " + ago(lastCheck.at) + ": " + lastCheck.summary
                : "No check has run yet."
          )
        )
      ),
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "What It Found (" + pending.length + ")"),
          h(CardDescription, null, st.inbox && st.inbox.note ? st.inbox.note : "What it finds is listed here.")
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          pending.length === 0
            ? h("p", { className: "hq-pro-empty" }, "Nothing is waiting on you.")
            : pending.map(function (offer) {
                return h(SuggestionCard, { key: offer.id, offer: offer, busy: busy, onRespond: onRespond, onCopied: onCopied });
              })
        )
      )
    );
  }

  function LoopCard({ lp, busy, onPatchLoop }) {
    const [editing, setEditing] = useState(false);
    const [title, setTitle] = useState(lp.title);
    const [kind, setKind] = useState(lp.kind);
    const [due, setDue] = useState("");
    const [dueTouched, setDueTouched] = useState(false);
    const [who, setWho] = useState(lp.counterparty || "");

    const closed = lp.status === "resolved" || lp.status === "dropped";
    const save = function (e) {
      e.preventDefault();
      if (!title.trim()) return;
      const patch = { title: title.trim(), kind: kind, counterparty: who.trim() };
      // The date is only sent when it was edited; an emptied field clears it.
      if (dueTouched) patch.due_at = due.trim();
      onPatchLoop(lp.id, patch).then(function (ok) {
        if (ok) setEditing(false);
      });
    };

    if (editing) {
      return h(
        "form",
        { className: "hq-pro-item", onSubmit: save },
        h(
          "div",
          { className: "hq-pro-field-grid" },
          h(
            "div",
            { className: "hq-pro-field" },
            h(Label, null, "What is open"),
            h(Input, { value: title, onChange: function (e) { setTitle(e.target.value); } })
          ),
          h(
            "div",
            { className: "hq-pro-field" },
            h(Label, null, "Kind"),
            h(
              Select,
              { value: kind, onValueChange: setKind },
              Object.keys(KIND_LABELS).map(function (k) {
                return h(SelectOption, { key: k, value: k }, KIND_LABELS[k]);
              })
            )
          ),
          h(
            "div",
            { className: "hq-pro-field" },
            h(Label, null, lp.due_at ? "New date (now " + when(lp.due_at) + "; empty removes it)" : "Date (optional)"),
            h(Input, {
              value: due,
              placeholder: "tomorrow, by Friday, or 2026-03-01",
              onChange: function (e) { setDue(e.target.value); setDueTouched(true); },
            })
          ),
          h(
            "div",
            { className: "hq-pro-field" },
            h(Label, null, "With whom (optional)"),
            h(Input, { value: who, onChange: function (e) { setWho(e.target.value); } })
          )
        ),
        h(
          "div",
          { className: "hq-pro-actions" },
          h(Button, { type: "submit", disabled: busy || !title.trim() }, "Save"),
          lp.due_at
            ? h(
                Button,
                {
                  type: "button",
                  outlined: true,
                  disabled: busy,
                  onClick: function () { setDue(""); setDueTouched(true); },
                },
                "Remove Date"
              )
            : null,
          h(Button, { type: "button", ghost: true, disabled: busy, onClick: function () { setEditing(false); } }, "Cancel")
        )
      );
    }

    return h(
      "div",
      { className: "hq-pro-item" },
      h(
        "div",
        { className: "hq-pro-item-head" },
        h("span", { className: "hq-pro-item-title" }, lp.title),
        h(
          "div",
          { className: "hq-pro-meta" },
          lp.owner === "bot"
            ? h(Badge, { tone: "default" }, "The bot said it would")
            : h(Badge, { tone: "secondary" }, KIND_LABELS[lp.kind] || lp.kind),
          lp.status !== "open" ? h(Badge, { tone: "secondary" }, STATUS_LABELS[lp.status] || lp.status) : null,
          lp.due_at ? h(Badge, { tone: "default" }, "Due " + when(lp.due_at)) : null,
          lp.counterparty ? h("span", null, "with " + lp.counterparty) : null,
          h("span", null, ago(lp.updated_at))
        )
      ),
      lp.detail && lp.detail !== lp.title ? h("p", { className: "hq-pro-text is-soft" }, lp.detail) : null,
      closed && lp.resolved_by
        ? h("div", { className: "hq-pro-meta" }, "Closed by " + (CLOSED_BY[lp.resolved_by] || lp.resolved_by) + ".")
        : null,
      lp.status === "snoozed" && lp.snoozed_until
        ? h("div", { className: "hq-pro-meta" }, "Comes back " + when(lp.snoozed_until) + ".")
        : null,
      lp.prep_artifact ? h("pre", { className: "hq-pro-draft" }, lp.prep_artifact) : null,
      h(
        "div",
        { className: "hq-pro-actions" },
        closed
          ? null
          : h(Button, { disabled: busy, onClick: function () { onPatchLoop(lp.id, { status: "resolved" }); } }, "Mark Done"),
        lp.status === "open"
          ? h(Button, { outlined: true, disabled: busy, onClick: function () { onPatchLoop(lp.id, { snooze_hours: 48 }); } }, "Snooze 2 Days")
          : h(Button, { outlined: true, disabled: busy, onClick: function () { onPatchLoop(lp.id, { status: "open" }); } }, "Reopen"),
        closed
          ? null
          : h(Button, { outlined: true, disabled: busy, onClick: function () { setEditing(true); } }, "Edit"),
        closed
          ? null
          : h(Button, { ghost: true, destructive: true, disabled: busy, onClick: function () { onPatchLoop(lp.id, { status: "dropped" }); } }, "Drop")
      )
    );
  }

  function OpenItemsTab({ overview, busy, onAddLoop, onPatchLoop, onAddWatch, onDeleteWatch }) {
    const [filter, setFilter] = useState("open");
    const [title, setTitle] = useState("");
    const [kind, setKind] = useState("waiting_on");
    const [due, setDue] = useState("");
    const [counterparty, setCounterparty] = useState("");
    const [watchLabel, setWatchLabel] = useState("");
    const [watchKind, setWatchKind] = useState("deadline");
    const [watchTarget, setWatchTarget] = useState("");
    const [watchEvery, setWatchEvery] = useState("24");

    const all = overview.loops || [];
    const loops = useMemo(
      function () {
        if (filter === "all") return all;
        return all.filter(function (lp) { return lp.status === filter; });
      },
      [all, filter]
    );
    const counts = useMemo(
      function () {
        const out = { all: all.length };
        all.forEach(function (lp) { out[lp.status] = (out[lp.status] || 0) + 1; });
        return out;
      },
      [all]
    );
    const watches = overview.watches || [];
    const watchWords = WATCH_KINDS[watchKind] || WATCH_KINDS.deadline;

    const submitLoop = function (e) {
      e.preventDefault();
      if (!title.trim()) return;
      onAddLoop({ kind: kind, title: title.trim(), due_at: due.trim() || null, counterparty: counterparty.trim() }).then(function (ok) {
        if (ok) {
          setTitle("");
          setDue("");
          setCounterparty("");
        }
      });
    };

    const submitWatch = function (e) {
      e.preventDefault();
      if (!watchLabel.trim()) return;
      const payload = { kind: watchKind, label: watchLabel.trim(), target: watchTarget.trim() };
      if (watchKind === "custom_timer") {
        const every = Number(watchEvery);
        if (!watchEvery.trim() || Number.isNaN(every) || every <= 0) return;
        payload.every_hours = every;
      }
      onAddWatch(payload).then(function (ok) {
        if (ok) {
          setWatchLabel("");
          setWatchTarget("");
        }
      });
    };

    return h(
      "div",
      { className: "hq-pro-section" },
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Add something that is open"),
          h(CardDescription, null, "Most are picked up from your chats. Add one here by hand. They are kept apart from your task list.")
        ),
        h(
          CardContent,
          null,
          h(
            "form",
            { onSubmit: submitLoop, className: "hq-pro-section" },
            h(
              "div",
              { className: "hq-pro-field-grid" },
              h(
                "div",
                { className: "hq-pro-field" },
                h(Label, null, "What is open"),
                h(Input, {
                  value: title,
                  placeholder: "Waiting on the shelving quote",
                  onChange: function (e) { setTitle(e.target.value); },
                })
              ),
              h(
                "div",
                { className: "hq-pro-field" },
                h(Label, null, "Kind"),
                h(
                  Select,
                  { value: kind, onValueChange: setKind },
                  Object.keys(KIND_LABELS).map(function (k) {
                    return h(SelectOption, { key: k, value: k }, KIND_LABELS[k]);
                  })
                )
              ),
              h(
                "div",
                { className: "hq-pro-field" },
                h(Label, null, "Date (optional)"),
                h(Input, {
                  value: due,
                  placeholder: "tomorrow, by Friday, or 2026-03-01",
                  onChange: function (e) { setDue(e.target.value); },
                })
              ),
              h(
                "div",
                { className: "hq-pro-field" },
                h(Label, null, "With whom (optional)"),
                h(Input, {
                  value: counterparty,
                  placeholder: "A person, team or company",
                  onChange: function (e) { setCounterparty(e.target.value); },
                })
              )
            ),
            h(
              "div",
              { className: "hq-pro-actions" },
              h(Button, { type: "submit", disabled: busy || !title.trim() }, "Add")
            )
          )
        )
      ),
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(
            "div",
            { className: "hq-pro-item-head" },
            h(CardTitle, null, "Open items"),
            h(
              "div",
              { className: "hq-pro-tabs" },
              ["open", "snoozed", "resolved", "dropped", "all"].map(function (key) {
                return h(
                  "button",
                  {
                    key: key,
                    type: "button",
                    className: "hq-pro-tab" + (filter === key ? " is-active" : ""),
                    onClick: function () { setFilter(key); },
                  },
                  STATUS_LABELS[key] + " (" + (counts[key] || 0) + ")"
                );
              })
            )
          )
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          loops.length === 0
            ? h("p", { className: "hq-pro-empty" }, filter === "open" ? "Nothing is open." : "Nothing here.")
            : loops.map(function (lp) {
                return h(LoopCard, { key: lp.id + ":" + lp.updated_at, lp: lp, busy: busy, onPatchLoop: onPatchLoop });
              })
        )
      ),
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Things to watch (" + watches.length + ")"),
          h(CardDescription, null, "Looked at on each hourly check. A suggestion is made when the date nears, the file changes, or the time comes round.")
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          h(
            "form",
            { onSubmit: submitWatch, className: "hq-pro-field-grid" },
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Name"),
              h(Input, {
                value: watchLabel,
                placeholder: "Release freeze",
                onChange: function (e) { setWatchLabel(e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Watch"),
              h(
                Select,
                { value: watchKind, onValueChange: setWatchKind },
                Object.keys(WATCH_KINDS).map(function (k) {
                  return h(SelectOption, { key: k, value: k }, WATCH_KINDS[k].label);
                })
              )
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, watchWords.target),
              h(Input, {
                value: watchTarget,
                placeholder: watchWords.hint,
                onChange: function (e) { setWatchTarget(e.target.value); },
              })
            ),
            watchKind === "custom_timer"
              ? h(
                  "div",
                  { className: "hq-pro-field" },
                  h(Label, null, "Every how many hours"),
                  h(Input, {
                    type: "number",
                    min: 1,
                    max: 720,
                    value: watchEvery,
                    onChange: function (e) { setWatchEvery(e.target.value); },
                  })
                )
              : null,
            h(
              "div",
              { className: "hq-pro-field is-end" },
              h(Button, { type: "submit", outlined: true, disabled: busy || !watchLabel.trim() }, "Add")
            )
          ),
          watches.length === 0
            ? h("p", { className: "hq-pro-empty" }, "Nothing is being watched.")
            : watches.map(function (w) {
                return h(
                  "div",
                  { key: w.id, className: "hq-pro-row" },
                  h(
                    "div",
                    null,
                    h("strong", { className: "hq-pro-strong" }, w.label),
                    h(
                      "div",
                      { className: "hq-pro-meta" },
                      ((WATCH_KINDS[w.kind] || {}).label || w.kind) + (w.target ? " · " + (dayWords(w.target) || w.target) : "") +
                        (w.last_triggered_at ? " · last raised " + ago(w.last_triggered_at) : "")
                    )
                  ),
                  h(
                    Button,
                    { ghost: true, destructive: true, disabled: busy, onClick: function () { onDeleteWatch(w.id); } },
                    "Remove"
                  )
                );
              })
        )
      )
    );
  }

  function learningLine(row) {
    const paused = row.auto_paused_until && new Date(row.auto_paused_until).getTime() > Date.now();
    if (paused) return "Paused until " + when(row.auto_paused_until) + ", after you dismissed three in a row.";
    if (Number(row.threshold) > 0.56) return "Suggested less often, because you turned some down. This eases off with time.";
    if (Number(row.threshold) < 0.54) return "Suggested a little more readily, because you took these up.";
    return "Suggested as usual.";
  }

  function HistoryTab({ overview, busy, onResetCalibration }) {
    const calibration = overview.calibration || [];
    const audit = overview.audit || [];
    // Paused or moved counts as something to show, even with no answers left on the books.
    const hasAnswers = function (row) {
      return Number(row.accepted || 0) + Number(row.edited || 0) + Number(row.snoozed || 0) + Number(row.dismissed || 0) +
        Number(row.ignored || 0) > 0 || learningLine(row) !== "Suggested as usual.";
    };
    const unanswered = calibration.filter(function (row) { return !hasAnswers(row); });

    return h(
      "div",
      { className: "hq-pro-section" },
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(
            "div",
            { className: "hq-pro-item-head" },
            h(
              "div",
              null,
              h(CardTitle, null, "What it has learned"),
              h(CardDescription, null, "Each kind of suggestion is made more or less often by what you do with it. One you never looked at counts for nothing.")
            ),
            h(Button, { outlined: true, disabled: busy, onClick: function () { onResetCalibration(null); } }, "Start Over")
          )
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          // A kind with nothing answered yet is one name in a line, not a card saying so.
          unanswered.length
            ? h(
                "div",
                { className: "hq-pro-text is-soft" },
                unanswered.length === calibration.length
                  ? "Nothing to learn from yet."
                  : "Nothing yet for: " +
                    unanswered.map(function (row) { return CATEGORY_LABELS[row.category] || row.category; }).join(", ") + "."
              )
            : null,
          h("div", { className: "hq-pro-grid-2" },
          calibration.filter(hasAnswers).map(function (row) {
            const paused = row.auto_paused_until && new Date(row.auto_paused_until).getTime() > Date.now();
            const taken = Number(row.accepted || 0) + Number(row.edited || 0);
            return h(
              "div",
              { key: row.category, className: "hq-pro-item" },
              h(
                "div",
                { className: "hq-pro-item-head" },
                h("strong", { className: "hq-pro-strong" }, CATEGORY_LABELS[row.category] || row.category),
                paused ? h(Badge, { tone: "warning" }, "Paused") : null
              ),
              h("div", { className: "hq-pro-text is-soft" }, learningLine(row)),
              h(
                "div",
                { className: "hq-pro-meta" },
                h("span", null, "Taken up: " + taken),
                h("span", null, "Snoozed: " + Number(row.snoozed || 0)),
                h("span", null, "Dismissed: " + Number(row.dismissed || 0)),
                h("span", null, "Never looked at: " + Number(row.ignored || 0))
              )
            );
          }))
        )
      ),
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Activity"),
          h(CardDescription, null, "What it noted, suggested, closed and stopped, and what each check cost.")
        ),
        h(
          CardContent,
          null,
          audit.length === 0
            ? h("p", { className: "hq-pro-empty" }, "Nothing yet.")
            : audit.map(function (entry) {
                return h(
                  "div",
                  { key: entry.id, className: "hq-pro-row" },
                  h(
                    "div",
                    null,
                    h("div", { className: "hq-pro-strong is-plain" }, entry.summary),
                    h(
                      "div",
                      { className: "hq-pro-meta" },
                      h("span", null, ago(entry.at)),
                      entry.tokens_used > 0 ? h("span", null, "used " + entry.tokens_used + " tokens") : null
                    )
                  ),
                  ACTIVITY_LABELS[entry.event_type]
                    ? h(Badge, { tone: entry.outcome === "blocked" ? "warning" : "secondary" }, ACTIVITY_LABELS[entry.event_type])
                    : null
                );
              })
        )
      )
    );
  }

  function SettingsTab({ settings, timezone, busy, onSave, onInvalid }) {
    const [draft, setDraft] = useState(settings);

    useEffect(
      function () {
        setDraft(settings);
      },
      [settings]
    );

    const setField = function (k, v) {
      setDraft(function (prev) { return Object.assign({}, prev, { [k]: v }); });
    };

    const toggleCategory = function (cat, enabled) {
      const current = new Set(draft.disabled_categories || []);
      if (enabled) current.delete(cat);
      else current.add(cat);
      setField("disabled_categories", Array.from(current));
    };

    // A number field holds what was typed. An empty or broken one stops the save instead of
    // becoming 0, which for "most suggestions a day" would silently turn everything off.
    const save = function () {
      const next = Object.assign({}, draft);
      for (let i = 0; i < NUMBER_FIELDS.length; i += 1) {
        const key = NUMBER_FIELDS[i][0];
        const raw = String(draft[key] === undefined || draft[key] === null ? "" : draft[key]).trim();
        const num = Number(raw);
        if (raw === "" || Number.isNaN(num)) {
          onInvalid("Give a number for “" + NUMBER_FIELDS[i][1] + "”.");
          return;
        }
        next[key] = Math.max(NUMBER_FIELDS[i][2], Math.min(NUMBER_FIELDS[i][3], Math.round(num)));
      }
      onSave(next);
    };

    return h(
      "div",
      { className: "hq-pro-section" },
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Settings"),
          h(CardDescription, null, "For this bot only. Whatever you choose, it only reads and tells you: nothing is sent to anyone else, booked, paid or changed. Times are on your Hermes clock (" + (timezone || "local") + ").")
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          h(
            "div",
            { className: "hq-pro-field-grid" },
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "How much it does"),
              h(
                Select,
                { value: draft.autonomy_mode, onValueChange: function (v) { setField("autonomy_mode", v); } },
                Object.keys(MODE_LABELS).map(function (m) {
                  return h(SelectOption, { key: m, value: m }, MODE_LABELS[m]);
                })
              )
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "When it finds something"),
              h(
                Select,
                { value: draft.finds_go_to || "chat", onValueChange: function (v) { setField("finds_go_to", v); } },
                Object.keys(PLACE_LABELS).map(function (place) {
                  return h(SelectOption, { key: place, value: place }, PLACE_LABELS[place]);
                })
              )
            ),
            NUMBER_FIELDS.map(function (field) {
              return h(
                "div",
                { key: field[0], className: "hq-pro-field" },
                h(Label, null, field[1]),
                h(Input, {
                  type: "number",
                  min: field[2],
                  max: field[3],
                  value: draft[field[0]] === undefined || draft[field[0]] === null ? "" : String(draft[field[0]]),
                  onChange: function (e) { setField(field[0], e.target.value); },
                })
              );
            }),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Quiet hours start"),
              h(Input, {
                value: draft.quiet_start,
                placeholder: "22:00",
                onChange: function (e) { setField("quiet_start", e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Quiet hours end"),
              h(Input, {
                value: draft.quiet_end,
                placeholder: "08:00",
                onChange: function (e) { setField("quiet_end", e.target.value); },
              })
            )
          ),
          h(
            "div",
            { className: "hq-pro-note" },
            "In quiet hours the bot does not message you and the check uses no model; what it finds waits until after."
          ),
          h(
            "div",
            { className: "hq-pro-section" },
            SWITCHES.map(function (row) {
              return h(
                "div",
                { key: row[0], className: "hq-pro-row" },
                h(
                  "div",
                  null,
                  h("div", { className: "hq-pro-strong is-plain" }, row[1]),
                  h("div", { className: "hq-pro-meta" }, row[2])
                ),
                h(Checkbox, {
                  checked: !!draft[row[0]],
                  onCheckedChange: function (checked) { setField(row[0], !!checked); },
                })
              );
            })
          ),
          h(
            "div",
            { className: "hq-pro-section" },
            h(Label, null, "Kinds of suggestion to make"),
            h(
              "div",
              { className: "hq-pro-grid-2" },
              Object.keys(CATEGORY_LABELS).map(function (cat) {
                const isOn = (draft.disabled_categories || []).indexOf(cat) === -1;
                return h(
                  "div",
                  { key: cat, className: "hq-pro-row" },
                  h("span", { className: "hq-pro-strong is-plain" }, CATEGORY_LABELS[cat]),
                  h(Checkbox, {
                    checked: isOn,
                    onCheckedChange: function (checked) { toggleCategory(cat, !!checked); },
                  })
                );
              })
            )
          ),
          h(
            "div",
            { className: "hq-pro-field-grid" },
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Watch for"),
              h("textarea", {
                className: "hq-pro-textarea",
                value: draft.focus_note || "",
                placeholder: "Replies I owe clients, anything with a date this week.",
                onChange: function (e) { setField("focus_note", e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Leave alone"),
              h("textarea", {
                className: "hq-pro-textarea",
                value: draft.skip_note || "",
                placeholder: "Family plans, passing ideas, routine updates.",
                onChange: function (e) { setField("skip_note", e.target.value); },
              })
            )
          ),
          h(
            "div",
            { className: "hq-pro-actions" },
            h(Button, { disabled: busy, onClick: save }, busy ? "Saving…" : "Save")
          )
        )
      )
    );
  }

  function ProactivityBetaPage() {
    const [overview, setOverview] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);
    const [tab, setTab] = useState("today");
    const [checking, setChecking] = useState(false);
    const { showToast, toast } = useToast ? useToast(3600) : { showToast: function () {}, toast: null };

    const load = useCallback(function () {
      setError(null);
      return SDK.fetchJSON(API + "/overview")
        .then(function (data) {
          setOverview(data);
          setLoading(false);
          // A check started earlier (or from another screen) is still going: keep watching it.
          if (data && data.check && data.check.running) setChecking(true);
        })
        .catch(function (err) {
          setError(err && err.message ? err.message : "Could not load Proactivity (Beta).");
          setLoading(false);
        });
    }, []);

    useEffect(function () {
      load();
    }, [load]);

    // A check runs on the gateway and can take a minute. The page asks how it is going
    // instead of holding one long request open, which a phone gives up on.
    useEffect(
      function () {
        if (!checking) return undefined;
        let stopped = false;
        let timer = null;
        let tries = 0;
        const finish = function (message, tone) {
          if (stopped) return;
          setChecking(false);
          if (message) showToast(message, tone || "success");
          load();
        };
        const ask = function () {
          tries += 1;
          SDK.fetchJSON(API + "/sweep")
            .then(function (res) {
              if (stopped) return;
              const check = (res && res.check) || {};
              if (check.running && tries < 160) {
                timer = setTimeout(ask, 1500);
              } else if (check.running) {
                finish("The check is still running. Look again in a minute.", "success");
              } else if (check.error) {
                finish(check.error, "error");
              } else {
                finish(checkLine(check.result), "success");
              }
            })
            .catch(function () {
              if (stopped) return;
              if (tries < 160) timer = setTimeout(ask, 3000);
              else finish("Could not reach the gateway to ask how the check went.", "error");
            });
        };
        timer = setTimeout(ask, 900);
        return function () {
          stopped = true;
          if (timer) clearTimeout(timer);
        };
      },
      [checking, load, showToast]
    );

    const call = useCallback(
      function (path, method, payload, done, failed) {
        setBusy(true);
        const options = { method: method };
        if (payload !== undefined) {
          options.headers = { "content-type": "application/json" };
          options.body = JSON.stringify(payload);
        }
        return SDK.fetchJSON(API + path, options)
          .then(function (res) {
            const message = typeof done === "function" ? done(res) : done;
            if (message) showToast(message, "success");
            return load().then(function () { return true; });
          })
          .catch(function (err) {
            showToast((err && err.message) || failed, "error");
            return false;
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const patchSettings = useCallback(
      function (patch, message) {
        return call("/settings", "PATCH", patch, message, "Could not save.");
      },
      [call]
    );

    const onRespond = useCallback(
      function (offerId, action, snoozeHours) {
        const said = { accept: "Marked done.", edited_accept: "Copied and marked done.", snooze: "Snoozed for a day.", dismiss: "Dismissed." };
        return call(
          "/offers/" + encodeURIComponent(offerId) + "/respond",
          "POST",
          { action: action, snooze_hours: snoozeHours || 24 },
          said[action] || "Saved.",
          "That did not go through."
        );
      },
      [call]
    );

    const onCopied = useCallback(
      function (copied) {
        if (!copied) showToast("Could not copy it. Select the draft and copy it by hand; nothing was marked done.", "error");
      },
      [showToast]
    );

    const onCheck = useCallback(
      function () {
        setBusy(true);
        return SDK.fetchJSON(API + "/sweep", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({}),
        })
          .then(function () {
            setChecking(true);
          })
          .catch(function (err) {
            showToast((err && err.message) || "The check could not start.", "error");
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [showToast]
    );

    const onScout = useCallback(
      function () {
        return call("/scout", "POST", {}, "Chief-of-Staff check-in started in this bot's chat.", "Could not start the check-in.");
      },
      [call]
    );

    const onAddLoop = useCallback(
      function (payload) {
        return call("/loops", "POST", payload, function (res) {
          return res && res.created ? "Added." : "That one is already listed.";
        }, "Could not add it.");
      },
      [call]
    );

    const onPatchLoop = useCallback(
      function (loopId, payload) {
        return call("/loops/" + encodeURIComponent(loopId), "PATCH", payload, "Saved.", "Could not save it.");
      },
      [call]
    );

    const onAddWatch = useCallback(
      function (payload) {
        return call("/watches", "POST", payload, "Now watching it.", "Could not add it.");
      },
      [call]
    );

    const onDeleteWatch = useCallback(
      function (watchId) {
        return call("/watches/" + encodeURIComponent(watchId), "DELETE", undefined, "Removed.", "Could not remove it.");
      },
      [call]
    );

    const onResetCalibration = useCallback(
      function (category) {
        return call("/calibration/reset", "POST", { category: category }, "Started over: every kind is suggested as usual.", "Could not reset.");
      },
      [call]
    );

    if (loading) {
      return h("div", { className: "hq-pro-page" }, h("p", { className: "hq-pro-empty" }, "Loading…"));
    }
    if (error || !overview) {
      return h(
        "div",
        { className: "hq-pro-page" },
        h(
          Card,
          null,
          h(CardHeader, null, h(CardTitle, null, "Could not load Proactivity (Beta)"), h(CardDescription, null, error || "Unknown error")),
          h(CardContent, null, h(Button, { onClick: load }, "Try Again"))
        )
      );
    }

    const st = overview.status;
    const enabled = !!st.enabled;
    const quietRange = st.settings.quiet_start + "–" + st.settings.quiet_end;
    const usage = usageFor(st);

    return h(
      "div",
      { className: "hq-pro-page" },
      Toast ? h(Toast, { toast: toast }) : null,
      h(
        "div",
        { className: "hq-pro-hero" },
        h(
          "div",
          { className: "hq-pro-hero-copy" },
          h(
            "div",
            { className: "hq-pro-title-row" },
            h("h1", { className: "hq-pro-title" }, "Proactivity"),
            h("span", { className: "hq-pro-beta-pill" }, "BETA · " + st.version)
          ),
          h(
            "p",
            { className: "hq-pro-subtitle" },
            "Keeps track of what is left open in your chats with this bot and speaks up when something is due. It only reads and tells you: nothing is sent to anyone else, booked, paid or deleted without you."
          )
        ),
        h(
          "div",
          { className: "hq-pro-hero-actions" },
          h(ToggleSwitch, {
            checked: enabled,
            disabled: busy,
            label: enabled ? "On" : "Off",
            onToggle: function () {
              patchSettings({ enabled: !enabled }, !enabled ? "Proactivity (Beta) is on." : "Proactivity (Beta) is off.");
            },
          })
        )
      ),
      !enabled
        ? h(OffExplainer, {
            saving: busy,
            onEnable: function () { patchSettings({ enabled: true }, "Proactivity (Beta) is on."); },
          })
        : h(
            React.Fragment,
            null,
            h(
              "div",
              { className: "hq-pro-stats" },
              h(
                "div",
                { className: "hq-pro-stat" },
                h("div", { className: "hq-pro-stat-label" }, "Finds waiting"),
                h("div", { className: "hq-pro-stat-value" }, String(st.counts.pending_offers)),
                h("div", { className: "hq-pro-stat-sub" }, whereWords(st.inbox))
              ),
              h(
                "div",
                { className: "hq-pro-stat" },
                h("div", { className: "hq-pro-stat-label" }, "Open items"),
                h("div", { className: "hq-pro-stat-value" }, String(st.counts.open_loops)),
                h("div", { className: "hq-pro-stat-sub" }, st.counts.snoozed_loops + " snoozed · " + st.counts.active_watches + " watched")
              ),
              h(
                "div",
                { className: "hq-pro-stat" },
                h("div", { className: "hq-pro-stat-label" }, usage.label),
                h("div", { className: "hq-pro-stat-value" }, usage.used + " of " + usage.limit),
                h("div", { className: "hq-pro-stat-sub" }, "Your limit is " + usage.limit + " a day")
              ),
              h(
                "div",
                { className: "hq-pro-stat" },
                h("div", { className: "hq-pro-stat-label" }, "Quiet hours"),
                h("div", { className: "hq-pro-stat-value" }, st.quiet_hours_active ? "On now" : "Not now"),
                h("div", { className: "hq-pro-stat-sub" }, quietRange + (st.timezone ? " · " + st.timezone : ""))
              )
            ),
            h(
              "div",
              { className: "hq-pro-tabs", role: "tablist" },
              [
                ["today", "Today (" + st.counts.pending_offers + ")"],
                ["loops", "Open Items (" + st.counts.open_loops + ")"],
                ["history", "History"],
                ["settings", "Settings"],
              ].map(function (entry) {
                return h(
                  "button",
                  {
                    key: entry[0],
                    type: "button",
                    role: "tab",
                    "aria-selected": tab === entry[0],
                    className: "hq-pro-tab" + (tab === entry[0] ? " is-active" : ""),
                    onClick: function () { setTab(entry[0]); },
                  },
                  entry[1]
                );
              })
            ),
            tab === "loops"
              ? h(OpenItemsTab, {
                  overview: overview,
                  busy: busy,
                  onAddLoop: onAddLoop,
                  onPatchLoop: onPatchLoop,
                  onAddWatch: onAddWatch,
                  onDeleteWatch: onDeleteWatch,
                })
              : tab === "history"
                ? h(HistoryTab, { overview: overview, busy: busy, onResetCalibration: onResetCalibration })
                : tab === "settings"
                  ? h(SettingsTab, {
                      settings: st.settings,
                      timezone: st.timezone,
                      busy: busy,
                      onSave: function (next) { patchSettings(next, "Saved."); },
                      onInvalid: function (message) { showToast(message, "error"); },
                    })
                  : h(TodayTab, {
                      overview: overview,
                      busy: busy,
                      checking: checking,
                      onRespond: onRespond,
                      onCheck: onCheck,
                      onScout: onScout,
                      onCopied: onCopied,
                    })
          )
    );
  }

  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, ProactivityBetaPage);
})();
