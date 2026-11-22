"""Terminal chat with the Bookly agent. Same run_turn() as the web UI.

    python cli.py            # chat
    python cli.py --trace    # chat and print each turn's trace

Commands: /new (new conversation), /reset (reset demo data), /tickets, /quit
"""

from __future__ import annotations

import argparse
import asyncio
import json

from bookly.agent.state import Session
from bookly.data import seed
from bookly.runtime import build_agent, require_key

DIM, BOLD, RED, GREEN, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[36m", "\033[0m"


def fmt_event(e: dict) -> str:
    p, t = e["payload"], e["type"]
    meta = ""
    if e.get("model"):
        meta = f"  [{e['model']} {e['latency_ms']}ms ${e['cost_usd']:.5f}]"
    if t == "input_guard":
        flags = [k for k in ("injection", "abuse") if p.get(k)] + ([p["sensitive"]] if p.get("sensitive") != "none" else [])
        body = ("FLAGGED " + ",".join(flags)) if flags else "clean"
        color = RED if flags else DIM
    elif t == "route":
        body = f"{p['aop']} mode={p['mode']} intents={p['intents']} conf={p['confidence']} confirm={p['confirmation']} writes={p['write_tools']}"
        body += f"  ({p['why']})"
        color = CYAN
    elif t == "executor":
        body = f"stop={p['stop_reason']} tools={p['tool_calls']} cache_read={p['cache_read']}"
        color = DIM
    elif t == "tool_call":
        body = f"{p['tool']}({json.dumps(p['args'])}) -> ok"
        color = GREEN
    elif t == "gate_denied":
        body = f"{p['tool']}({json.dumps(p['args'])}) -> DENIED {p['verdict']}: {p['result'].get('message')}"
        color = RED
    elif t == "tool_error":
        body = f"{p.get('tool', '')} error: {p.get('result', p.get('error'))}"
        color = RED
    elif t == "output_check" and p.get("skipped"):
        body, color = f"supervisor skipped: {p['reason']}", DIM
    elif t == "output_check":
        body = f"{p.get('stage')}: " + ("ok" if p.get("ok", p.get("grounded")) else f"FAILED {p.get('issues') or p.get('unsupported')}")
        color = DIM if p.get("ok", p.get("grounded")) else RED
    elif t == "escalation":
        body = f"ticket #{p['ticket_id']} -> {p['queue']} ({p['trigger']}, by {p['by']})"
        color = BOLD
    else:
        return ""
    return f"{color}  · {t:<12} {body}{meta}{RESET}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()
    require_key()
    agent = build_agent()
    session = Session()

    print(f"{BOLD}Bookly support{RESET}  (/new, /reset, /tickets, /quit)")
    for sc in seed.SCENARIOS:
        print(f"{DIM}  [{sc['card']}] {sc['customer']}: order {sc['order_number']}, {sc['email']}{RESET}")
    while True:
        try:
            text = input(f"\n{BOLD}you>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text == "/quit":
            return
        if text == "/new":
            session = Session()
            print(f"{DIM}new conversation{RESET}")
            continue
        if text == "/reset":
            agent.repo.reset()
            session = Session()
            print(f"{DIM}demo data reset; new conversation{RESET}")
            continue
        if text == "/tickets":
            for t in agent.repo.tickets():
                print(f"#{t['id']} [{t['queue']}/{t['priority']}] {t['customer']}: {t['summary']}")
            continue
        result = await agent.run_turn(session, text)
        if args.trace:
            for e in result.events:
                line = fmt_event(e)
                if line:
                    print(line)
            cost = sum(e["cost_usd"] or 0 for e in result.events)
            latency = sum(e["latency_ms"] or 0 for e in result.events if e["type"] in ("executor", "route"))
            print(f"{DIM}  · turn {session.turn}: ${cost:.5f}, ~{latency}ms model time{RESET}")
        print(f"\n{BOLD}bookly>{RESET} {result.reply}")


if __name__ == "__main__":
    asyncio.run(main())
