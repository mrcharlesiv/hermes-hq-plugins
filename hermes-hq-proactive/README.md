# hermes-hq-proactive (Beta)

> **Beta · Off by default.** Turning this plugin on is explicit and reversible at any time. While turned off, it injects zero prompt tokens, hides all of its tools, writes nothing during chats, and exits background checks immediately with zero LLM usage.

A standalone [Hermes](https://github.com/NousResearch/hermes-agent) plugin with a built-in **Proactivity (Beta)** page that works in both **Hermes Desktop** and **Hermes HQ** (iPhone & Mac).

## What it does

- **In-Turn Next-Step Anticipation (`pre_llm_call`)** — Encourages agents to perform obvious read-only checks or stage drafts instead of stopping at generic suggestions, and to surface concrete next moves as structured offers.
- **Open-Loop & Commitment Radar (`post_llm_call`)** — Deterministically tracks soft commitments (*"I'll revisit this Friday"*, *"waiting on vendor quote"*, *"prep for Monday's demo"*) separate from your formal task list, injecting only the 1–3 relevant loops on future turns.
- **Repetition-to-Automation Spotter** — Notices when you ask for the same multi-step workflow 3+ times and offers to turn it into a reusable skill or scheduled check.
- **Signal-Gated Idle Sweeps, Read-Only Chat Scan & Unified Daily Brief** — Incrementally scans recent non-cron chats in `state.db` (strictly read-only, watermarked) plus deterministic checks (overdue/due-soon loops, stale `waiting_on` items, file/deadline/timer watches). Spends **zero LLM tokens** when nothing new or overdue exists; stages drafts and batches low-urgency items into one quiet Daily Brief.
- **Hard Safety Fence (`pre_tool_call`)** — Background sweeps are strictly read-only and draft-only. Sending, emailing, booking, buying, paying, posting, merging, pushing, or deleting are blocked in code.
- **Adaptive Self-Calibration** — Every offer records `accept`, `edited_accept`, `snooze`, or `dismiss`. Snoozing is neutral; dismissing raises the category's confidence bar and auto-cools it for 7 days after 3 consecutive dismissals.

## Three Tiers of Proactivity

| Tier | Behaviour | Active in |
| :--- | :--- | :--- |
| **Tier 0 · Silent Prep** | Read-only verification, checklists, and staged drafts | `observe`, `prep`, `full` |
| **Tier 1 · Offer / Brief** | Structured offers & Daily Brief (bounded by daily offer budget) | `prep`, `full` |
| **Tier 2 · Urgent Interrupt** | Time-sensitive deadline/blocker alerts (budgeted, paused during quiet hours) | `full` only |

## Toggling On / Off

Install into a Hermes profile and enable the plugin; its runtime feature switch starts **OFF**:

- **In Hermes HQ / Hermes Desktop**: Open **Proactivity (Beta)** from Plugin Pages (or Capabilities › Plugins › `hermes-hq-proactive`) and flip the toggle.
- **In any chat**: `/proactivity on`, `/proactivity off`, `/proactivity status`, `/proactivity mode prep`, `/proactivity brief`, `/proactivity sweep`.
- **From CLI**: `hermes proactive enable`, `hermes proactive disable`, `hermes proactive status`.

## Testing

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest -q -p no:cacheprovider hermes-hq-proactive/tests
node --check hermes-hq-proactive/dashboard/dist/index.js
```
