#!/usr/bin/env python3
"""How bots ask in app chats: question cards (clarify) against questions left as text at the end of a reply.

    python3 scripts/ask_cards_report.py --since 2026-10-15 --until 2026-10-29T15:00
    python3 scripts/ask_cards_report.py --since 2026-10-29T15:00 --cards     # also every card, for review

Reads each bot's state.db read-only (desktop sessions, helper runs left out). A "text question" is a reply that ends
the turn (no tool calls) with a question in its last two sentences; "offer" when it opens like "Want me to…".
"""
import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".hermes"
OFFER = re.compile(r"\b(want me to|should i|shall i|do you want me to|would you like me to|i can also|happy to)\b", re.I)


def stamp(text):
    return datetime.fromisoformat(text).timestamp() if text else None


def bots():
    yield "hermes", HOME / "state.db"
    for db in sorted((HOME / "profiles").glob("*/state.db")):
        yield db.parent.name, db


def tail_question(text):
    text = (text or "").strip()
    if not text:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", text[-600:])
    tail = " ".join(sentences[-2:])
    return tail if "?" in tail else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until")
    ap.add_argument("--cards", action="store_true")
    a = ap.parse_args()
    lo, hi = stamp(a.since), stamp(a.until) or 1e12
    total = {"turns": 0, "cards": 0, "text": 0, "offers": 0}
    print(f"{'bot':24} {'turns':>6} {'cards':>6} {'text Q':>7} {'offers':>7}  card outcomes")
    for name, db in bots():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "select m.session_id, m.role, m.content, m.tool_calls, m.tool_name, m.timestamp from messages m "
            "join sessions s on s.id = m.session_id where s.source = 'desktop' and s.parent_session_id is null "
            "and m.timestamp >= ? and m.timestamp < ? order by m.id", (lo, hi)).fetchall()
        conn.close()
        turns = cards = text_q = offers = 0
        outcomes, asked = {}, []
        for sid, role, content, tool_calls, tool_name, ts in rows:
            if role == "user":
                turns += 1
            elif role == "assistant" and (not tool_calls or tool_calls in ("[]", "null")):
                q = tail_question(content)
                if q:
                    text_q += 1
                    offers += bool(OFFER.search(q))
            elif role == "tool" and tool_name == "clarify":
                cards += 1
                try:
                    result = json.loads(content or "{}")
                except ValueError:
                    result = {}
                outcome = result.get("outcome") or "error"
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                first = (result.get("responses") or [{}])[0]
                asked.append((ts, sid, outcome, first.get("question"), first.get("choices_offered"), first.get("user_response")))
        if turns or cards:
            print(f"{name:24} {turns:6} {cards:6} {text_q:7} {offers:7}  {outcomes or ''}")
            if a.cards:
                for ts, sid, outcome, question, choices, answer in asked:
                    when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                    print(f"    {when} {sid} [{outcome}] {question!r} {choices or ''} -> {answer!r}")
        total["turns"] += turns; total["cards"] += cards; total["text"] += text_q; total["offers"] += offers
    print(f"{'all':24} {total['turns']:6} {total['cards']:6} {total['text']:7} {total['offers']:7}")


if __name__ == "__main__":
    main()
