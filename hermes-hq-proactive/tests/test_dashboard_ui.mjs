import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(__dirname, "..");
const bundlePath = path.join(pluginRoot, "dashboard", "dist", "index.js");
const version = JSON.parse(fs.readFileSync(path.join(pluginRoot, "dashboard", "manifest.json"), "utf8")).version;

// Words the 0.2.0 page used that only its author understood. None may come back.
const JARGON = [/Tier \d/, /ctx\.llm/, /\bconf\b/, /Bar \d+%/, /Deterministic/, /Signal-Gated/i, /sweep/i, /calibration/i,
  /open[- ]loop/i, /\bLLM\b/, /watermark/i];

/** A tiny stand-in for React and the Hermes plugin SDK: enough to render the page to a tree. */
function harness({ clipboard } = {}) {
  const registered = new Map();
  const calls = [];
  const toasts = [];
  let overview = null;
  let stateIdx = 0;
  let states = [];

  const React = {
    Fragment: Symbol("Fragment"),
    createElement(type, props, ...children) {
      const flat = children.length <= 1 ? children[0] : children;
      return { type, props: Object.assign({}, props, { children: flat }) };
    },
  };
  const hooks = {
    useState(init) {
      const i = stateIdx++;
      if (!(i in states)) states[i] = typeof init === "function" ? init() : init;
      return [states[i], (next) => { states[i] = typeof next === "function" ? next(states[i]) : next; }];
    },
    useEffect() {},
    useCallback(fn) { return fn; },
    useMemo(fn) { return fn(); },
    useToast() { return { toast: null, showToast(message, tone) { toasts.push({ message, tone }); } }; },
  };
  const pass = (tag) => (props) => React.createElement(tag, props, props && props.children);
  const components = {
    Card: pass("div"), CardHeader: pass("div"), CardTitle: pass("h3"), CardDescription: pass("p"),
    CardContent: pass("div"), Badge: pass("span"), Button: pass("button"), Checkbox: pass("input"),
    Input: pass("input"), Label: pass("label"), Select: pass("select"), SelectOption: pass("option"),
    Toast: () => null,
  };
  const sandbox = {
    window: {
      __HERMES_PLUGINS__: { register(name, Component) { registered.set(name, Component); } },
      __HERMES_PLUGIN_SDK__: {
        React,
        hooks,
        components,
        utils: { cn: (...a) => a.filter(Boolean).join(" "), isoTimeAgo: () => "just now" },
        fetchJSON: async (url, options) => {
          calls.push({ url, method: (options && options.method) || "GET", body: options && options.body ? JSON.parse(options.body) : null });
          return overview;
        },
      },
    },
    navigator: clipboard ? { clipboard } : {},
    Date, Object, Array, Number, String, Math, Set, JSON, Promise, encodeURIComponent,
    setTimeout, clearTimeout,
  };
  vm.runInNewContext(fs.readFileSync(bundlePath, "utf8"), sandbox, { filename: bundlePath });
  const Page = registered.get("hermes-hq-proactive");

  /** Render with the page's own state seeded: overview, loading, error, busy, tab, checking. */
  function render(data, tab = "today", more = []) {
    overview = data;
    stateIdx = 0;
    states = [data, false, null, false, tab, false, ...more];
    return expand(Page({}));
  }
  function expand(node) {
    if (node == null || typeof node !== "object") return node;
    if (Array.isArray(node)) return node.map(expand);
    if (typeof node.type === "function") return expand(node.type(node.props || {}));
    return { type: node.type, props: Object.assign({}, node.props, { children: expand(node.props && node.props.children) }) };
  }
  return { registered, render, calls, toasts };
}

function text(node) {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join(" ");
  return text(node.props && node.props.children);
}

function all(node, pred, out = []) {
  if (node == null || typeof node !== "object") return out;
  if (Array.isArray(node)) { node.forEach((n) => all(n, pred, out)); return out; }
  if (pred(node)) out.push(node);
  all(node.props && node.props.children, pred, out);
  return out;
}

const button = (tree, label) => all(tree, (n) => n.type === "button" && text(n).trim() === label)[0];
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

function overviewOff() {
  return {
    status: {
      plugin: "hermes-hq-proactive", version, beta: true, enabled: false, autonomy_mode: "prep",
      quiet_hours_active: false, timezone: "America/Chicago", local_time: "14:00",
      budgets: { offers_used_24h: 0, offers_limit_24h: 6, interrupts_used_24h: 0, interrupts_limit_24h: 2,
        messages_used_24h: 1, messages_limit_24h: 3 },
      counts: { pending_offers: 0, open_loops: 0, snoozed_loops: 0, active_watches: 0 },
      last_check: null, brief_job: "off",
      inbox: { goes_to: "chat", state: "chat", to_chat: true, to_ideas: false,
        note: "The bot looks into what it finds and messages you in its chat." },
      settings: {
        enabled: false, autonomy_mode: "prep", quiet_start: "22:00", quiet_end: "08:00", brief_hour: 8,
        daily_offer_budget: 6, daily_interrupt_budget: 2, stale_waiting_days: 3, scan_lookback_hours: 72,
        max_context_loops: 3, daily_brief_enabled: true, finds_go_to: "chat", chat_lookup: true,
        daily_message_budget: 3, disabled_categories: [],
      },
    },
    check: { running: false, result: null, error: null },
    brief: null, pending_offers: [], loops: [], watches: [], calibration: [], audit: [],
  };
}

function overviewOn() {
  const data = overviewOff();
  data.status.enabled = true;
  data.status.settings.enabled = true;
  data.status.brief_job = "on";
  data.status.counts = { pending_offers: 2, open_loops: 2, snoozed_loops: 0, active_watches: 1 };
  data.status.last_check = { at: "2026-03-04T19:00:00Z", summary: "Checked: read 4 new messages in 1 chat, made 1 suggestion (sent to Tasks › Ideas)." };
  data.brief = {
    brief_date: "2026-03-04", summary: "2 suggestions waiting and 1 open item. First: Send the signed lease back.",
    highlights: [{ kind: "offer", id: "of_1", title: "Send the signed lease back", note: "Due in about 5 h.", tier: 1, where: "ideas" }],
  };
  data.pending_offers = [
    { id: "of_1", category: "prepared_draft", tier: 1, title: "Send the signed lease back", summary: "Due in about 5 h.",
      draft_content: "", delivered_to: "ideas", idea_id: "idea1", confidence: 0.82, created_at: "2026-03-04T12:00:00Z" },
    { id: "of_2", category: "follow_up", tier: 2, title: "Gone quiet with Acme: the shelving quote", summary: "Nothing has moved on this for 4 day(s).",
      draft_content: "Hello, checking in on the shelving quote.", delivered_to: "page", confidence: 0.78, created_at: "2026-03-04T12:00:00Z" },
    { id: "of_3", category: "next_step", tier: 1, title: "Check the train times for Saturday", summary: "You said you would look them up.",
      draft_content: "", delivered_to: "chat", said_at: "2026-03-04T18:00:00Z", confidence: 0.8, created_at: "2026-03-04T17:00:00Z" },
    { id: "of_4", category: "next_step", tier: 1, title: "Compare the two venue quotes", summary: "Both quotes are in.",
      draft_content: "", delivered_to: "chat", said_at: null, confidence: 0.8, created_at: "2026-03-04T19:30:00Z" },
  ];
  data.loops = [
    { id: "lp_1", kind: "waiting_on", owner: "external", title: "Waiting on the shelving quote", status: "open", counterparty: "Acme",
      due_at: null, detail: "", updated_at: "2026-03-01T12:00:00Z" },
    { id: "lp_2", kind: "follow_up", owner: "bot", title: "Check back on the venue options", status: "open", counterparty: "",
      due_at: "2026-03-06T23:00:00Z", detail: "", updated_at: "2026-03-03T12:00:00Z" },
    { id: "lp_3", kind: "commitment", owner: "user", title: "Renew the parking permit", status: "resolved", counterparty: "",
      due_at: null, detail: "", resolved_by: "scan", updated_at: "2026-03-03T12:00:00Z" },
    { id: "lp_4", kind: "commitment", owner: "user", title: "Order new shelf brackets", status: "dropped", counterparty: "",
      due_at: null, detail: "", resolved_by: "idea_dismissed", updated_at: "2026-03-03T12:00:00Z" },
  ];
  data.watches = [{ id: "wt_1", kind: "deadline", label: "Release freeze", target: "2026-03-10" }];
  data.calibration = [
    { category: "follow_up", threshold: 0.7, accepted: 1, edited: 0, snoozed: 0, dismissed: 3, ignored: 4, auto_paused_until: null },
    { category: "next_step", threshold: 0.55, accepted: 0, edited: 0, snoozed: 0, dismissed: 0, ignored: 0, auto_paused_until: null },
  ];
  data.audit = [{ id: 1, event_type: "sweep_completed", summary: "Checked: made 1 suggestion.", tokens_used: 140, at: "2026-03-04T19:00:00Z", outcome: "completed" }];
  return data;
}

test("off: says so plainly and offers to turn it on", () => {
  const ui = harness();
  assert.equal(ui.registered.has("hermes-hq-proactive"), true);
  const page = text(ui.render(overviewOff()));
  assert.match(page, /BETA/);
  assert.match(page, /Proactivity is off/);
  assert.match(page, /Turn On Proactivity \(Beta\)/);
  assert.match(page, /Only reads and suggests/);
  for (const word of JARGON) assert.doesNotMatch(page, word);
});

test("on: every tab renders in plain words", () => {
  const ui = harness();
  const pages = {};
  for (const tab of ["today", "loops", "history", "settings"]) {
    pages[tab] = text(ui.render(overviewOn(), tab));
    for (const word of JARGON) assert.doesNotMatch(pages[tab], word, `${tab}: ${word}`);
  }
  // Today: where finds go, the brief, the last check, and an honest label on each button.
  assert.match(pages.today, /The bot looks into what it finds and messages you in its chat/);
  assert.match(pages.today, /Said in the bot's chat/);
  assert.match(pages.today, /Next message/);
  assert.match(pages.today, /Messages in 24 hours\s*1 of 3/);
  assert.match(pages.today, /Finds waiting\s*2\s*The bot messages you/);
  assert.match(pages.today, /posted in this bot's chat at 8:00/);
  assert.match(pages.today, /Last check just now: Checked: read 4 new messages/);
  assert.match(pages.today, /In Tasks › Ideas/);
  assert.match(pages.today, /Time-sensitive/);
  assert.match(pages.today, /Marking it done only records your answer/);
  assert.match(pages.today, /Check Now/);
  assert.match(pages.today, /Today's brief · \w{3}, \w{3} 4/);        // "Wed, Mar 4", not 2026-03-04
  assert.doesNotMatch(pages.today, /2026-03-04/);
  assert.match(pages.today, /Your limit is \d+ a day/);
  // Open items: all five filters with counts, who said it, and no Dropped item hiding.
  assert.match(pages.loops, /Open \(2\)/);
  assert.match(pages.loops, /Dropped \(1\)/);
  assert.match(pages.loops, /The bot said it would/);
  assert.match(pages.loops, /Things to watch \(1\)/);
  // History: what it learned in words, and what a check cost.
  assert.match(pages.history, /Suggested less often, because you turned some down/);
  assert.match(pages.history, /Never looked at: 4/);
  assert.match(pages.history, /Nothing yet for: /);                    // kinds with no answers are one line
  assert.doesNotMatch(pages.loops, /Said I would\s*The bot said it would/);   // one badge for whose it is
  assert.match(pages.history, /used 140 tokens/);
  // Settings: the clock it uses, where finds go, and the limit on messages.
  assert.match(pages.settings, /America\/Chicago/);
  assert.match(pages.settings, /When it finds something\s*The bot messages me in its chat\s*Make it a card in Tasks › Ideas\s*Keep it on this page/);
  assert.match(pages.settings, /Most messages a day/);
  assert.match(pages.settings, /Look into it before messaging me/);
  assert.match(pages.settings, /Morning brief in the bot's chat/);
});

test("a suggestion in Tasks › Ideas can only be dismissed here; one on the page can be answered", () => {
  const ui = harness();
  const tree = ui.render(overviewOn());
  const cards = all(tree, (n) => n.props && n.props.className === "hq-pro-item");
  const inIdeas = cards.find((c) => /Send the signed lease back/.test(text(c)));
  const onPage = cards.find((c) => /shelving quote/.test(text(c)));
  assert.deepEqual(all(inIdeas, (n) => n.type === "button").map((b) => text(b).trim()), ["Dismiss"]);
  assert.deepEqual(all(onPage, (n) => n.type === "button").map((b) => text(b).trim()),
    ["Mark Done", "Copy Draft & Mark Done", "Snooze a Day", "Dismiss"]);
});

test("something the bot said in its chat can be rated here; one still to be said can only be dismissed", () => {
  const ui = harness();
  const cards = all(ui.render(overviewOn()), (n) => n.props && n.props.className === "hq-pro-item");
  const said = cards.find((c) => /train times/.test(text(c)));
  const next = cards.find((c) => /venue quotes/.test(text(c)));
  assert.match(text(said), /The bot messaged you about this/);
  assert.deepEqual(all(said, (n) => n.type === "button").map((b) => text(b).trim()), ["It Helped", "Not Helpful"]);
  assert.match(text(next), /will look into this and message you at the next check/);
  assert.deepEqual(all(next, (n) => n.type === "button").map((b) => text(b).trim()), ["Dismiss"]);

  // Where finds go elsewhere, the tiles say so and count finds, not messages.
  const data = overviewOn();
  data.status.inbox = { goes_to: "ideas", state: "ok", to_chat: false, to_ideas: true, note: "What it finds goes to Tasks › Ideas in the app." };
  const page = text(harness().render(data));
  assert.match(page, /Shown in Tasks › Ideas/);
  assert.match(page, /Found in 24 hours\s*0 of 6/);
});

test("a closed item says who closed it and can be reopened", () => {
  const ui = harness();
  // The Open Items tab's own first state is its filter.
  const done = ui.render(overviewOn(), "loops", ["resolved"]);
  assert.match(text(done), /Closed by a check, which saw in your chats that it was done/);
  assert.ok(button(done, "Reopen"));
  const dropped = ui.render(overviewOn(), "loops", ["dropped"]);
  assert.match(text(dropped), /Closed by you, by dismissing it in Tasks › Ideas/);
});

test("copying a draft marks it done only when the copy worked", async () => {
  const failing = harness({ clipboard: { writeText: () => Promise.reject(new Error("denied")) } });
  button(failing.render(overviewOn()), "Copy Draft & Mark Done").props.onClick();
  await settle();
  assert.equal(failing.calls.filter((c) => c.method === "POST").length, 0);
  assert.match(failing.toasts.at(-1).message, /Could not copy it/);

  const working = harness({ clipboard: { writeText: () => Promise.resolve() } });
  button(working.render(overviewOn()), "Copy Draft & Mark Done").props.onClick();
  await settle();
  const posted = working.calls.filter((c) => c.method === "POST");
  assert.equal(posted.length, 1);
  assert.match(posted[0].url, /offers\/of_2\/respond$/);
  assert.equal(posted[0].body.action, "edited_accept");
});

test("an emptied number is refused instead of being saved as 0", async () => {
  const ui = harness();
  const data = overviewOn();
  const emptied = Object.assign({}, data.status.settings, { daily_offer_budget: "" });
  // The Settings tab's own first state is its draft.
  button(ui.render(data, "settings", [emptied]), "Save").props.onClick();
  await settle();
  assert.equal(ui.calls.filter((c) => c.method === "PATCH").length, 0);
  assert.match(ui.toasts.at(-1).message, /Give a number for “Most finds a day”/);

  const typed = Object.assign({}, data.status.settings, { daily_offer_budget: "4", brief_hour: "99" });
  button(ui.render(data, "settings", [typed]), "Save").props.onClick();
  await settle();
  const saved = ui.calls.filter((c) => c.method === "PATCH");
  assert.equal(saved.length, 1);
  assert.equal(saved[0].body.daily_offer_budget, 4);
  assert.equal(saved[0].body.brief_hour, 23);
});

test("Check Now starts the check and does not wait for it", async () => {
  const ui = harness();
  button(ui.render(overviewOn()), "Check Now").props.onClick();
  await settle();
  const posted = ui.calls.filter((c) => c.method === "POST");
  assert.equal(posted.length, 1);
  assert.match(posted[0].url, /\/sweep$/);
});
