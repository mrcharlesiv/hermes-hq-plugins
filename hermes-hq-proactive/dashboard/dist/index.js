/* hermes-hq-proactive (Beta) — Dashboard & Hermes HQ Plugin Page UI
 * Uses window.__HERMES_PLUGIN_SDK__ (SDK 1.1.0) so it runs identically in
 * Hermes Desktop and Hermes HQ (iPhone & Mac) with zero build dependencies.
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

  const CATEGORY_LABELS = {
    next_step: "Next Steps",
    follow_up: "Follow-Ups",
    pre_event: "Pre-Event Prep",
    prepared_draft: "Prepared Drafts",
    pattern_automation: "Pattern Automation",
    stale_check: "Stale Checks",
  };

  const MODE_LABELS = {
    observe: "Observe & Brief (Tier 0 only)",
    prep: "Prep & Offer (Tier 0–1 · Recommended)",
    full: "Prep + Urgent Interrupt (Tier 0–2)",
  };

  function tierLabel(tier) {
    if (tier === 0) return "Tier 0 · Silent Prep";
    if (tier === 2) return "Tier 2 · Urgent";
    return "Tier 1 · Offer";
  }

  function fmtPercent(val) {
    if (val === null || val === undefined || Number.isNaN(val)) return "—";
    return Math.round(Number(val) * 100) + "%";
  }

  function safeIsoAgo(iso) {
    if (!iso) return "never";
    try {
      return isoTimeAgo ? isoTimeAgo(iso) : iso.slice(0, 16).replace("T", " ");
    } catch {
      return iso.slice(0, 10);
    }
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
      h("span", null, label || (checked ? "Enabled" : "Off"))
    );
  }

  function OffExplainer({ saving, onEnable }) {
    return h(
      Card,
      null,
      h(
        CardHeader,
        null,
        h(CardTitle, null, "Proactivity is off by default (Beta)"),
        h(
          CardDescription,
          null,
          "While off, this plugin adds zero prompt tokens, hides its tools, and runs zero background checks. Toggle it on whenever you want this bot to think and prepare one step ahead."
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
            h("strong", null, "Tier 0 · Silent Prep"),
            h("p", null, "Read-only research, checklists, and drafts staged quietly ahead of deadlines or meetings.")
          ),
          h(
            "div",
            { className: "hq-pro-tier-box" },
            h("strong", null, "Tier 1 · Structured Offers & Brief"),
            h("p", null, "One-tap next-step offers, waiting-on follow-up drafts, and a single daily brief within your daily budget.")
          ),
          h(
            "div",
            { className: "hq-pro-tier-box" },
            h("strong", null, "Hard Safety Fence & Self-Calibration"),
            h("p", null, "Never sends, books, buys, pays, posts, or deletes on its own. Categories you dismiss automatically cool down.")
          )
        ),
        h(
          "div",
          { className: "hq-pro-actions" },
          h(
            Button,
            { disabled: saving, onClick: onEnable },
            saving ? "Enabling…" : "Turn On Proactivity (Beta)"
          )
        )
      )
    );
  }

  function BriefAndOffersTab({ overview, busy, onRespond, onSweep }) {
    const brief = overview.brief;
    const pending = overview.pending_offers || [];

    const copyDraft = useCallback(function (text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(function () {});
      }
    }, []);

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
              h(CardTitle, null, brief ? "Daily Brief · " + brief.brief_date : "Daily Brief"),
              h(
                CardDescription,
                null,
                brief
                  ? "Generated " + safeIsoAgo(brief.created_at) + " (" + brief.generated_by + ")"
                  : "Batches open loops and prepared drafts into one quiet summary."
              )
            ),
            h(
              "div",
              { className: "hq-pro-actions" },
              h(
                Button,
                { outlined: true, disabled: busy, onClick: function () { onSweep(false); } },
                busy ? "Checking…" : "Run Signal Check"
              ),
              h(
                Button,
                { outlined: true, disabled: busy, onClick: function () { onSweep(true); } },
                "Refresh Brief"
              )
            )
          )
        ),
        h(
          CardContent,
          null,
          brief
            ? h(
                "div",
                { className: "hq-pro-section" },
                h("p", { style: { margin: 0, lineHeight: 1.5 } }, brief.summary),
                (brief.highlights || []).length > 0
                  ? h(
                      "div",
                      { className: "hq-pro-section" },
                      brief.highlights.map(function (item, idx) {
                        return h(
                          "div",
                          { key: item.id || idx, className: "hq-pro-row" },
                          h(
                            "div",
                            null,
                            h("strong", { style: { fontSize: "13.5px" } }, item.title),
                            h("div", { className: "hq-pro-meta" }, item.kind.toUpperCase() + " · " + item.note)
                          )
                        );
                      })
                    )
                  : null
              )
            : h("p", { className: "hq-pro-empty" }, "No brief generated yet. Click Refresh Brief or wait for your scheduled brief hour.")
        )
      ),
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Ready & Pending Offers (" + pending.length + ")"),
          h(CardDescription, null, "Approve, snooze, or dismiss. Every answer tunes future confidence thresholds.")
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          pending.length === 0
            ? h("p", { className: "hq-pro-empty" }, "Queue is clear — nothing waiting for your decision right now.")
            : pending.map(function (offer) {
                return h(
                  "div",
                  { key: offer.id, className: "hq-pro-item" },
                  h(
                    "div",
                    { className: "hq-pro-item-head" },
                    h("span", { className: "hq-pro-item-title" }, offer.title),
                    h(
                      "div",
                      { className: "hq-pro-meta" },
                      h(Badge, { tone: "secondary" }, CATEGORY_LABELS[offer.category] || offer.category),
                      h(Badge, { tone: offer.tier >= 2 ? "warning" : "default" }, tierLabel(offer.tier)),
                      h("span", null, fmtPercent(offer.confidence) + " conf · " + safeIsoAgo(offer.created_at))
                    )
                  ),
                  h("p", { style: { margin: 0, fontSize: "13.5px", lineHeight: 1.45 } }, offer.summary),
                  offer.why ? h("div", { className: "hq-pro-meta" }, "Why now: " + offer.why) : null,
                  offer.draft_content ? h("pre", { className: "hq-pro-draft" }, offer.draft_content) : null,
                  offer.suggested_action
                    ? h("div", { className: "hq-pro-meta" }, "Suggested next step: " + offer.suggested_action)
                    : null,
                  h(
                    "div",
                    { className: "hq-pro-actions" },
                    h(
                      Button,
                      { disabled: busy, onClick: function () { onRespond(offer.id, "accept"); } },
                      "Approve"
                    ),
                    offer.draft_content
                      ? h(
                          Button,
                          {
                            outlined: true,
                            disabled: busy,
                            onClick: function () {
                              copyDraft(offer.draft_content);
                              onRespond(offer.id, "edited_accept");
                            },
                          },
                          "Copy Draft & Accept"
                        )
                      : null,
                    h(
                      Button,
                      { outlined: true, disabled: busy, onClick: function () { onRespond(offer.id, "snooze", 24); } },
                      "Snooze 24h"
                    ),
                    h(
                      Button,
                      { ghost: true, destructive: true, disabled: busy, onClick: function () { onRespond(offer.id, "dismiss"); } },
                      "Dismiss"
                    )
                  )
                );
              })
        )
      )
    );
  }

  function LoopsAndWatchesTab({ overview, busy, onAddLoop, onPatchLoop, onAddWatch, onDeleteWatch }) {
    const [filter, setFilter] = useState("open");
    const [title, setTitle] = useState("");
    const [kind, setKind] = useState("waiting_on");
    const [due, setDue] = useState("");
    const [counterparty, setCounterparty] = useState("");
    const [watchLabel, setWatchLabel] = useState("");
    const [watchKind, setWatchKind] = useState("deadline");
    const [watchTarget, setWatchTarget] = useState("");

    const loops = useMemo(
      function () {
        const all = overview.loops || [];
        if (filter === "all") return all;
        return all.filter(function (lp) { return lp.status === filter; });
      },
      [overview.loops, filter]
    );

    const watches = overview.watches || [];

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
      onAddWatch({ kind: watchKind, label: watchLabel.trim(), target: watchTarget.trim() }).then(function (ok) {
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
          h(CardTitle, null, "Track an Open Loop"),
          h(CardDescription, null, "Soft commitments, waiting-on items, or upcoming prep — kept separate from your task list.")
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
                h(Label, null, "What's open?"),
                h(Input, {
                  value: title,
                  placeholder: "Waiting on vendor quote for Q2",
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
                  h(SelectOption, { value: "waiting_on" }, "Waiting On"),
                  h(SelectOption, { value: "follow_up" }, "Follow-Up"),
                  h(SelectOption, { value: "pre_event" }, "Pre-Event Prep"),
                  h(SelectOption, { value: "commitment" }, "Commitment")
                )
              ),
              h(
                "div",
                { className: "hq-pro-field" },
                h(Label, null, "Due / Check by (optional)"),
                h(Input, {
                  value: due,
                  placeholder: "tomorrow, Friday, or 2026-12-05",
                  onChange: function (e) { setDue(e.target.value); },
                })
              ),
              h(
                "div",
                { className: "hq-pro-field" },
                h(Label, null, "Counterparty (optional)"),
                h(Input, {
                  value: counterparty,
                  placeholder: "Vendor / team / person",
                  onChange: function (e) { setCounterparty(e.target.value); },
                })
              )
            ),
            h(
              "div",
              { className: "hq-pro-actions" },
              h(Button, { type: "submit", disabled: busy || !title.trim() }, "Add Open Loop")
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
            h(CardTitle, null, "Open-Loop Radar (" + loops.length + ")"),
            h(
              "div",
              { className: "hq-pro-tabs" },
              ["open", "snoozed", "resolved", "all"].map(function (st) {
                return h(
                  "button",
                  {
                    key: st,
                    type: "button",
                    className: "hq-pro-tab" + (filter === st ? " is-active" : ""),
                    onClick: function () { setFilter(st); },
                  },
                  st[0].toUpperCase() + st.slice(1)
                );
              })
            )
          )
        ),
        h(
          CardContent,
          { className: "hq-pro-section" },
          loops.length === 0
            ? h("p", { className: "hq-pro-empty" }, "No loops in this view.")
            : loops.map(function (lp) {
                return h(
                  "div",
                  { key: lp.id, className: "hq-pro-item" },
                  h(
                    "div",
                    { className: "hq-pro-item-head" },
                    h("span", { className: "hq-pro-item-title" }, lp.title),
                    h(
                      "div",
                      { className: "hq-pro-meta" },
                      h(Badge, { tone: "secondary" }, lp.kind.replace("_", " ")),
                      lp.due_at ? h(Badge, { tone: "default" }, "Due " + lp.due_at.slice(0, 10)) : null,
                      lp.counterparty ? h("span", null, "with " + lp.counterparty) : null,
                      h("span", null, safeIsoAgo(lp.updated_at))
                    )
                  ),
                  lp.detail && lp.detail !== lp.title
                    ? h("p", { style: { margin: 0, fontSize: "13px", opacity: 0.85 } }, lp.detail)
                    : null,
                  lp.prep_artifact ? h("pre", { className: "hq-pro-draft" }, lp.prep_artifact) : null,
                  h(
                    "div",
                    { className: "hq-pro-actions" },
                    lp.status !== "resolved"
                      ? h(Button, { disabled: busy, onClick: function () { onPatchLoop(lp.id, { status: "resolved" }); } }, "Mark Resolved")
                      : h(Button, { outlined: true, disabled: busy, onClick: function () { onPatchLoop(lp.id, { status: "open" }); } }, "Reopen"),
                    lp.status === "open"
                      ? h(Button, { outlined: true, disabled: busy, onClick: function () { onPatchLoop(lp.id, { snooze_hours: 48 }); } }, "Snooze 2d")
                      : null,
                    lp.status !== "dropped"
                      ? h(Button, { ghost: true, destructive: true, disabled: busy, onClick: function () { onPatchLoop(lp.id, { status: "dropped" }); } }, "Drop")
                      : null
                  )
                );
              })
        )
      ),
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Deterministic Watches (" + watches.length + ")"),
          h(CardDescription, null, "Evaluated cheaply during idle sweeps — zero LLM tokens until a watch trips.")
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
              h(Label, null, "Watch label"),
              h(Input, {
                value: watchLabel,
                placeholder: "Release freeze deadline",
                onChange: function (e) { setWatchLabel(e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Kind"),
              h(
                Select,
                { value: watchKind, onValueChange: setWatchKind },
                h(SelectOption, { value: "deadline" }, "Deadline"),
                h(SelectOption, { value: "file_change" }, "File Change"),
                h(SelectOption, { value: "custom_timer" }, "Periodic Check-In")
              )
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Target (date or path)"),
              h(Input, {
                value: watchTarget,
                placeholder: "2026-12-10 or /path/to/file",
                onChange: function (e) { setWatchTarget(e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field", style: { justifyContent: "flex-end" } },
              h(Button, { type: "submit", outlined: true, disabled: busy || !watchLabel.trim() }, "Add Watch")
            )
          ),
          watches.length === 0
            ? h("p", { className: "hq-pro-empty" }, "No active watches yet.")
            : watches.map(function (w) {
                return h(
                  "div",
                  { key: w.id, className: "hq-pro-row" },
                  h(
                    "div",
                    null,
                    h("strong", { style: { fontSize: "13.5px" } }, w.label),
                    h("div", { className: "hq-pro-meta" }, w.kind + (w.target ? " · " + w.target : ""))
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

  function CalibrationAndAuditTab({ overview, busy, onResetCalibration }) {
    const calibration = overview.calibration || [];
    const audit = overview.audit || [];

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
              h(CardTitle, null, "Adaptive Calibration"),
              h(CardDescription, null, "Dismissed categories automatically raise their confidence bar and cool down after 3 consecutive dismissals.")
            ),
            h(
              Button,
              { outlined: true, disabled: busy, onClick: function () { onResetCalibration(null); } },
              "Reset All"
            )
          )
        ),
        h(
          CardContent,
          { className: "hq-pro-grid-2" },
          calibration.map(function (row) {
            const pct = Math.round(Number(row.threshold || 0.55) * 100);
            const paused = row.auto_paused_until && new Date(row.auto_paused_until).getTime() > Date.now();
            return h(
              "div",
              { key: row.category, className: "hq-pro-item" },
              h(
                "div",
                { className: "hq-pro-item-head" },
                h("strong", { style: { fontSize: "14px" } }, CATEGORY_LABELS[row.category] || row.category),
                paused
                  ? h(Badge, { tone: "warning" }, "Auto-cooled")
                  : h(Badge, { tone: "secondary" }, "Bar " + pct + "%")
              ),
              h("div", { className: "hq-pro-meter" }, h("div", { className: "hq-pro-meter-fill", style: { width: pct + "%" } })),
              h(
                "div",
                { className: "hq-pro-meta" },
                h("span", null, "Accepted: " + (row.accepted + row.edited)),
                h("span", null, "Snoozed: " + row.snoozed),
                h("span", null, "Dismissed: " + row.dismissed),
                h("span", null, "Rate: " + fmtPercent(row.acceptance_rate))
              ),
              row.last_reason ? h("div", { className: "hq-pro-meta" }, row.last_reason) : null
            );
          })
        )
      ),
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Audit & Cost Trail"),
          h(CardDescription, null, "Every proactive wake-up, blocked unsafe call, offer, and token cost is recorded transparently.")
        ),
        h(
          CardContent,
          null,
          audit.length === 0
            ? h("p", { className: "hq-pro-empty" }, "No proactive activity recorded yet.")
            : audit.map(function (entry) {
                return h(
                  "div",
                  { key: entry.id, className: "hq-pro-row" },
                  h(
                    "div",
                    null,
                    h("div", { style: { fontSize: "13.5px", fontWeight: 500 } }, entry.summary),
                    h(
                      "div",
                      { className: "hq-pro-meta" },
                      h("span", null, entry.event_type),
                      entry.trigger ? h("span", null, "via " + entry.trigger) : null,
                      entry.tokens_used > 0 ? h("span", null, entry.tokens_used + " tok") : null,
                      h("span", null, safeIsoAgo(entry.at))
                    )
                  ),
                  entry.outcome ? h(Badge, { tone: entry.outcome === "blocked" ? "warning" : "secondary" }, entry.outcome) : null
                );
              })
        )
      )
    );
  }

  function SettingsTab({ settings, busy, onSave }) {
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

    return h(
      "div",
      { className: "hq-pro-section" },
      h(
        Card,
        null,
        h(
          CardHeader,
          null,
          h(CardTitle, null, "Autonomy & Budgets"),
          h(CardDescription, null, "Tune how proactive this bot profile may be. Hard fence always forbids unapproved external actions.")
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
              h(Label, null, "Autonomy Mode"),
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
              h(Label, null, "Daily Offer Budget"),
              h(Input, {
                type: "number",
                min: 0,
                max: 30,
                value: String(draft.daily_offer_budget),
                onChange: function (e) { setField("daily_offer_budget", Number(e.target.value)); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Daily Interrupt Budget (Tier 2)"),
              h(Input, {
                type: "number",
                min: 0,
                max: 10,
                value: String(draft.daily_interrupt_budget),
                onChange: function (e) { setField("daily_interrupt_budget", Number(e.target.value)); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Quiet Hours Start"),
              h(Input, {
                value: draft.quiet_start,
                placeholder: "22:00",
                onChange: function (e) { setField("quiet_start", e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Quiet Hours End"),
              h(Input, {
                value: draft.quiet_end,
                placeholder: "08:00",
                onChange: function (e) { setField("quiet_end", e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Stale 'Waiting On' Days"),
              h(Input, {
                type: "number",
                min: 1,
                max: 30,
                value: String(draft.stale_waiting_days),
                onChange: function (e) { setField("stale_waiting_days", Number(e.target.value)); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Chat Scan Lookback (Hours)"),
              h(Input, {
                type: "number",
                min: 1,
                max: 336,
                value: String(draft.scan_lookback_hours || 72),
                onChange: function (e) { setField("scan_lookback_hours", Number(e.target.value)); },
              })
            )
          ),
          h(
            "div",
            { className: "hq-pro-section" },
            [
              ["in_turn_enabled", "In-turn next-step anticipation"],
              ["loop_radar_enabled", "Automatic open-loop & commitment radar"],
              ["scan_recent_chats", "Scan recent chats for open loops & follow-ups (read-only)"],
              ["idle_sweep_enabled", "Signal-gated idle sweeps (zero tokens when quiet)"],
              ["daily_brief_enabled", "Unified daily brief"],
              ["pattern_detection_enabled", "Repetition-to-automation spotter"],
              ["allow_llm_in_sweep", "Allow bounded LLM drafting during sweeps (ctx.llm)"],
            ].map(function (pair) {
              return h(
                "div",
                { key: pair[0], className: "hq-pro-row" },
                h("span", { style: { fontSize: "13.5px" } }, pair[1]),
                h(Checkbox, {
                  checked: !!draft[pair[0]],
                  onCheckedChange: function (checked) { setField(pair[0], !!checked); },
                })
              );
            })
          ),
          h(
            "div",
            { className: "hq-pro-section" },
            h(Label, null, "Enabled Offer Categories"),
            h(
              "div",
              { className: "hq-pro-grid-2" },
              Object.keys(CATEGORY_LABELS).map(function (cat) {
                const isOn = (draft.disabled_categories || []).indexOf(cat) === -1;
                return h(
                  "div",
                  { key: cat, className: "hq-pro-row" },
                  h("span", { style: { fontSize: "13px" } }, CATEGORY_LABELS[cat]),
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
              h(Label, null, "Focus Note (what to proactively watch)"),
              h("textarea", {
                className: "hq-pro-textarea",
                value: draft.focus_note || "",
                placeholder: "Example: Release blockers, unanswered client follow-ups, prep notes before Monday standup.",
                onChange: function (e) { setField("focus_note", e.target.value); },
              })
            ),
            h(
              "div",
              { className: "hq-pro-field" },
              h(Label, null, "Skip Note (what to never nudge about)"),
              h("textarea", {
                className: "hq-pro-textarea",
                value: draft.skip_note || "",
                placeholder: "Example: Personal reminders, casual brainstorms, or routine dependency bumps.",
                onChange: function (e) { setField("skip_note", e.target.value); },
              })
            )
          ),
          h(
            "div",
            { className: "hq-pro-actions" },
            h(
              Button,
              { disabled: busy, onClick: function () { onSave(draft); } },
              busy ? "Saving…" : "Save Proactivity Settings"
            )
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
    const [tab, setTab] = useState("brief");
    const { showToast, toast } = useToast ? useToast(3200) : { showToast: function () {}, toast: null };

    const load = useCallback(function () {
      setError(null);
      return SDK.fetchJSON(API + "/overview")
        .then(function (data) {
          setOverview(data);
          setLoading(false);
        })
        .catch(function (err) {
          setError(err && err.message ? err.message : "Could not load Proactivity (Beta).");
          setLoading(false);
        });
    }, []);

    useEffect(function () {
      load();
    }, [load]);

    const patchSettings = useCallback(
      function (patch, toastMsg) {
        setBusy(true);
        return SDK.fetchJSON(API + "/settings", {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(patch),
        })
          .then(function () {
            if (toastMsg) showToast(toastMsg, "success");
            return load();
          })
          .catch(function (err) {
            showToast((err && err.message) || "Could not save settings.", "error");
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const onRespond = useCallback(
      function (offerId, action, snoozeHours) {
        setBusy(true);
        return SDK.fetchJSON(API + "/offers/" + encodeURIComponent(offerId) + "/respond", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ action: action, snooze_hours: snoozeHours || 24 }),
        })
          .then(function () {
            showToast("Recorded (" + action.replace("_", " ") + ") and updated calibration.", "success");
            return load();
          })
          .catch(function (err) {
            showToast((err && err.message) || "Action failed.", "error");
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const onSweep = useCallback(
      function (forceBrief) {
        setBusy(true);
        return SDK.fetchJSON(API + "/sweep", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ force_brief: !!forceBrief }),
        })
          .then(function (res) {
            if (res && res.overview) setOverview(res.overview);
            else return load();
            const parts = [];
            if (res.chats_scanned > 0) parts.push(res.chats_scanned + " chat(s) scanned");
            if (res.loops_captured > 0) parts.push(res.loops_captured + " loop(s) captured");
            parts.push((res.created_offers || 0) + " new offer(s)");
            showToast(
              res.quiet
                ? res.chats_scanned > 0
                  ? "Checked " + res.chats_scanned + " recent chat(s) — nothing crossed threshold."
                  : "Signal check quiet — nothing crossed threshold (0 tokens)."
                : "Sweep complete: " + parts.join(", ") + ".",
              "success"
            );
          })
          .catch(function (err) {
            showToast((err && err.message) || "Sweep failed.", "error");
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const onAddLoop = useCallback(
      function (payload) {
        setBusy(true);
        return SDK.fetchJSON(API + "/loops", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        })
          .then(function (res) {
            showToast(res.created ? "Open loop tracked." : "Merged into matching open loop.", "success");
            return load().then(function () { return true; });
          })
          .catch(function (err) {
            showToast((err && err.message) || "Could not add loop.", "error");
            return false;
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const onPatchLoop = useCallback(
      function (loopId, payload) {
        setBusy(true);
        return SDK.fetchJSON(API + "/loops/" + encodeURIComponent(loopId), {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        })
          .then(function () {
            showToast("Loop updated.", "success");
            return load();
          })
          .catch(function (err) {
            showToast((err && err.message) || "Could not update loop.", "error");
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const onAddWatch = useCallback(
      function (payload) {
        setBusy(true);
        return SDK.fetchJSON(API + "/watches", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        })
          .then(function () {
            showToast("Watch added.", "success");
            return load().then(function () { return true; });
          })
          .catch(function (err) {
            showToast((err && err.message) || "Could not add watch.", "error");
            return false;
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const onDeleteWatch = useCallback(
      function (watchId) {
        setBusy(true);
        return SDK.fetchJSON(API + "/watches/" + encodeURIComponent(watchId), { method: "DELETE" })
          .then(function () {
            showToast("Watch removed.", "success");
            return load();
          })
          .catch(function (err) {
            showToast((err && err.message) || "Could not remove watch.", "error");
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    const onResetCalibration = useCallback(
      function (category) {
        setBusy(true);
        return SDK.fetchJSON(API + "/calibration/reset", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ category: category }),
        })
          .then(function () {
            showToast("Calibration thresholds reset.", "success");
            return load();
          })
          .catch(function (err) {
            showToast((err && err.message) || "Reset failed.", "error");
          })
          .finally(function () {
            setBusy(false);
          });
      },
      [load, showToast]
    );

    if (loading) {
      return h("div", { className: "hq-pro-page" }, h("p", { className: "hq-pro-empty" }, "Loading Proactivity (Beta)…"));
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
            h("span", { className: "hq-pro-beta-pill" }, "BETA · v" + st.version),
            h("span", { className: "hq-pro-state-pill" + (enabled ? " is-on" : "") }, enabled ? "Active" : "Off by default")
          ),
          h(
            "p",
            { className: "hq-pro-subtitle" },
            "Helps agents anticipate next steps, track open loops, and stage read-only drafts & briefs — never executing external side effects without your approval."
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
              patchSettings({ enabled: !enabled }, !enabled ? "Proactivity (Beta) enabled." : "Proactivity (Beta) turned off.");
            },
          })
        )
      ),
      !enabled
        ? h(OffExplainer, {
            saving: busy,
            onEnable: function () { patchSettings({ enabled: true }, "Proactivity (Beta) enabled."); },
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
                h("div", { className: "hq-pro-stat-label" }, "Pending Offers"),
                h("div", { className: "hq-pro-stat-value" }, String(st.counts.pending_offers)),
                h("div", { className: "hq-pro-stat-sub" }, "Mode: " + st.autonomy_mode)
              ),
              h(
                "div",
                { className: "hq-pro-stat" },
                h("div", { className: "hq-pro-stat-label" }, "Open Loops"),
                h("div", { className: "hq-pro-stat-value" }, String(st.counts.open_loops)),
                h("div", { className: "hq-pro-stat-sub" }, st.counts.snoozed_loops + " snoozed · " + st.counts.active_watches + " watches")
              ),
              h(
                "div",
                { className: "hq-pro-stat" },
                h("div", { className: "hq-pro-stat-label" }, "24h Offer Budget"),
                h("div", { className: "hq-pro-stat-value" }, st.budgets.offers_used_24h + " / " + st.budgets.offers_limit_24h),
                h("div", { className: "hq-pro-stat-sub" }, st.budgets.interrupts_used_24h + "/" + st.budgets.interrupts_limit_24h + " urgent interrupts")
              ),
              h(
                "div",
                { className: "hq-pro-stat" },
                h("div", { className: "hq-pro-stat-label" }, "Quiet Hours"),
                h("div", { className: "hq-pro-stat-value" }, st.quiet_hours_active ? "Quiet" : "Open"),
                h("div", { className: "hq-pro-stat-sub" }, st.settings.quiet_start + "–" + st.settings.quiet_end)
              )
            ),
            h(
              "div",
              { className: "hq-pro-tabs", role: "tablist" },
              [
                ["brief", "Brief & Ready (" + st.counts.pending_offers + ")"],
                ["loops", "Open Loops (" + st.counts.open_loops + ")"],
                ["calibration", "Calibration & Log"],
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
            tab === "brief"
              ? h(BriefAndOffersTab, { overview: overview, busy: busy, onRespond: onRespond, onSweep: onSweep })
              : tab === "loops"
                ? h(LoopsAndWatchesTab, {
                    overview: overview,
                    busy: busy,
                    onAddLoop: onAddLoop,
                    onPatchLoop: onPatchLoop,
                    onAddWatch: onAddWatch,
                    onDeleteWatch: onDeleteWatch,
                  })
                : tab === "calibration"
                  ? h(CalibrationAndAuditTab, { overview: overview, busy: busy, onResetCalibration: onResetCalibration })
                  : h(SettingsTab, {
                      settings: st.settings,
                      busy: busy,
                      onSave: function (next) { patchSettings(next, "Proactivity settings saved."); },
                    })
          )
    );
  }

  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, ProactivityBetaPage);
})();
