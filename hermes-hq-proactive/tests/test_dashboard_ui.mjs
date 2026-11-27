import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(__dirname, "..");
const bundlePath = path.join(pluginRoot, "dashboard", "dist", "index.js");

function makeTreeText(node) {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(makeTreeText).join(" ");
  if (typeof node === "object") {
    if (typeof node.type === "function") {
      return makeTreeText(node.type(node.props || {}));
    }
    return makeTreeText(node.props && node.props.children);
  }
  return "";
}

test("dashboard bundle registers cleanly and renders OFF (Beta) and ON states", () => {
  const source = fs.readFileSync(bundlePath, "utf8");
  const registered = new Map();

  let stateIdx = 0;
  let states = [];
  const effects = [];

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
      if (!(i in states)) {
        states[i] = typeof init === "function" ? init() : init;
      }
      return [states[i], (next) => { states[i] = typeof next === "function" ? next(states[i]) : next; }];
    },
    useEffect(fn) { effects.push(fn); },
    useCallback(fn) { return fn; },
    useMemo(fn) { return fn(); },
    useRef(val) { return { current: val }; },
    useToast() { return { toast: null, showToast() {} }; },
  };

  const pass = (tag) => (props) => React.createElement(tag, props, props && props.children);
  const components = {
    Card: pass("div"),
    CardHeader: pass("div"),
    CardTitle: pass("h3"),
    CardDescription: pass("p"),
    CardContent: pass("div"),
    Badge: pass("span"),
    Button: pass("button"),
    Checkbox: pass("input"),
    Input: pass("input"),
    Label: pass("label"),
    Select: pass("select"),
    SelectOption: pass("option"),
    Toast: () => null,
  };

  const fakeOverviewOff = {
    status: {
      plugin: "hermes-hq-proactive",
      version: "0.2.0-beta.2",
      beta: true,
      enabled: false,
      autonomy_mode: "prep",
      quiet_hours_active: false,
      budgets: { offers_used_24h: 0, offers_limit_24h: 6, interrupts_used_24h: 0, interrupts_limit_24h: 2 },
      counts: { pending_offers: 0, open_loops: 0, snoozed_loops: 0, active_watches: 0 },
      settings: { enabled: false, autonomy_mode: "prep", quiet_start: "22:00", quiet_end: "08:00" },
    },
    brief: null,
    pending_offers: [],
    loops: [],
    watches: [],
    calibration: [],
    audit: [],
  };

  const sandbox = {
    window: {
      __HERMES_PLUGINS__: {
        register(name, Component) { registered.set(name, Component); },
      },
      __HERMES_PLUGIN_SDK__: {
        React,
        hooks,
        components,
        utils: { cn: (...a) => a.filter(Boolean).join(" "), isoTimeAgo: () => "just now" },
        fetchJSON: async () => fakeOverviewOff,
      },
    },
    navigator: {},
    Date,
    Object,
    Array,
    Number,
    String,
    Math,
    Set,
    JSON,
    encodeURIComponent,
  };

  vm.runInNewContext(source, sandbox, { filename: bundlePath });

  assert.equal(registered.has("hermes-hq-proactive"), true);
  const Page = registered.get("hermes-hq-proactive");

  // Render OFF state (simulate loaded overview with enabled=false)
  stateIdx = 0;
  states = [fakeOverviewOff, false, null, false, "brief"];
  const offText = makeTreeText(Page({}));
  assert.match(offText, /BETA/);
  assert.match(offText, /Off by default/);
  assert.match(offText, /Turn On Proactivity \(Beta\)/);

  // Render ON state (simulate loaded overview with enabled=true + 1 pending offer)
  const fakeOverviewOn = structuredClone(fakeOverviewOff);
  fakeOverviewOn.status.enabled = true;
  fakeOverviewOn.status.counts.pending_offers = 1;
  fakeOverviewOn.pending_offers = [
    {
      id: "of_1",
      category: "follow_up",
      tier: 1,
      title: "Follow up on Q2 vendor quote",
      summary: "Untouched for 4 days.",
      draft_content: "Hi — checking in on the Q2 quote.",
      confidence: 0.82,
      created_at: "2026-11-27T12:00:00Z",
    },
  ];
  stateIdx = 0;
  states = [fakeOverviewOn, false, null, false, "brief"];
  const onText = makeTreeText(Page({}));
  assert.match(onText, /Active/);
  assert.match(onText, /Follow up on Q2 vendor quote/);
  assert.match(onText, /Copy Draft & Accept/);
});
