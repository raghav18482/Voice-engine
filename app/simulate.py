"""Persona harness: runs scripted customers through the real engine (no audio, no API key).

Run:  python -m app.simulate
Each persona states the outcome we expect; the harness reports pass/fail, turns, re-asks
and fields captured. These numbers are the robustness evidence for the demo.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

from .engine import CallSession
from .guardrails import gate

LEADS = {l["id"]: l for l in json.loads((Path(__file__).resolve().parent.parent / "data" / "leads.json").read_text())}

PERSONAS = [
    {"name": "Cooperative, new lead", "lead": "1001", "expect": "submitted", "turns": [
        "yes that's fine", "yeah sure go ahead", "it's for my home", "just switching, looking for a better deal",
        "two zero zero zero", "yes", "no", "electricity and gas", "we're with AGL", "no", "no",
        "yes I have a pension card", "about two hundred and fifty dollars", "option one", "Priya Sharma", "yes",
        "twelfth of March nineteen eighty five", "yes", "yes that's right", "yep",
        "12 George Street Sydney NSW 2000", "yes", "yes I agree"]},
    {"name": "Resumes mid-journey, corrects email", "lead": "1002", "expect": "submitted", "turns": [
        "yep", "sure", "just electricity", "origin", "yes we have solar", "no", "no", "skip", "the second one",
        "James Lee", "correct", "03/07/1979", "yes", "no, it's james dot lee at gmial dot com", "yes",
        "yes", "4 Pitt Street Sydney", "yes", "yes"]},
    {"name": "Moving house, interrupts with 'hang on'", "lead": "1003", "expect": "submitted", "turns": [
        "yes", "yes", "hang on a sec", "about one eighty dollars", "the cheapest one", "Mei Chen", "yes", "hold on",
        "5 May 1992", "yes", "yes", "yes", "Unit 3 18 Collins Street Melbourne VIC", "yes", "next monday", "yes",
        "yes I agree"]},
    {"name": "Frustrated repeat caller (brief example)", "lead": "1003", "expect": "handoff:anger", "turns": [
        "yes", "look I've already told three of you my details, this is the second call today",
        "honestly I'm done with this, just stop calling me"]},
    {"name": "Busy, wants a callback", "lead": "1001", "expect": "callback", "turns": [
        "yes", "I'm driving right now, can you call me later", "tomorrow after 5pm"]},
    {"name": "Not interested", "lead": "1002", "expect": "declined", "turns": [
        "yeah okay", "no thanks, I'm not interested"]},
    {"name": "Asks for a person", "lead": "1002", "expect": "handoff:asks", "turns": [
        "yes", "yes", "can I just talk to a real person please"]},
    {"name": "Wants to pay by card now", "lead": "1003", "expect": "handoff:sensitive", "turns": [
        "yes", "yes", "can I just pay with my credit card now", "no I want to pay by card now"]},
    {"name": "Reads out a card number", "lead": "1003", "expect": "handoff:sensitive", "turns": [
        "yes", "yes", "my card is 4111 1111 1111 1111"]},
    {"name": "Keeps asking for advice", "lead": "1002", "expect": "handoff:off_script", "turns": [
        "yes", "yes", "electricity", "AGL", "no", "no", "no", "skip", "which one is best for me",
        "what would you recommend"]},
    {"name": "Mishears date of birth 3 times", "lead": "1002", "expect": "handoff:confusion", "turns": [
        "yes", "yes", "electricity", "origin", "no", "no", "no", "skip", "one", "James Lee", "yes",
        "banana", "purple monkey dishwasher", "try again", "the thing"]},
    {"name": "Asks 'are you a robot?' then completes", "lead": "1002", "expect": "submitted", "turns": [
        "yes", "wait, are you a robot?", "okay yes", "gas", "AGL", "no", "no", "no", "skip", "two",
        "James Lee", "yes", "3 July 1979", "yes", "yes", "yes", "4 Pitt Street Sydney", "yes", "yes"]},
    {"name": "Refuses recording", "lead": "1001", "expect": "no_consent", "turns": [
        "no, I don't want to be recorded"]},
    {"name": "Embedded network (ineligible to switch)", "lead": "1001", "expect": "handoff:sensitive", "turns": [
        "yes", "yes", "home", "switching", "2000", "yes", "yes we pay the building"]},
    {"name": "WA household", "lead": "1006", "expect": "ineligible", "turns": [
        "yes", "yes", "home", "switching", "six thousand", "yes"]},
]


async def run_persona(p: dict, today: date | None = None) -> dict:
    lead = LEADS[p["lead"]]
    s = CallSession(lead, use_llm=False, simulated=True, today=today or date(2026, 9, 19))
    s.start()
    used = 0
    for u in p["turns"]:
        if s.phase in ("ended", "handoff_pending", "human"):
            break
        await s.handle(u, confidence=0.9)
        used += 1
    outcome = s.outcome or f"stuck:{s.phase}"
    m = s.metrics()
    return {"persona": p["name"], "lead": p["lead"], "expected": p["expect"], "actual": outcome,
            "pass": outcome == p["expect"], "turns": used, "reasks": s.reasks,
            "fields_by_ai": m["fields_captured_by_ai"], "missing": s.missing() if not outcome.startswith("sub") else [],
            "reference": (s.submission or {}).get("result", {}).get("reference"),
            "transcript": s.transcript, "events": s.events}


async def run_all() -> dict:
    results = [await run_persona(p) for p in PERSONAS]
    blocked = []
    for lid in ("1004", "1005"):
        g = gate(LEADS[lid])
        blocked.append({"lead": lid, "allowed": g["allowed"],
                        "failed": [c["check"] for c in g["checks"] if not c["ok"]]})
    passed = sum(r["pass"] for r in results)
    return {"passed": passed, "total": len(results), "results": results, "gate": blocked}


def main():
    out = asyncio.run(run_all())
    w = max(len(r["persona"]) for r in out["results"])
    for r in out["results"]:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{mark}  {r['persona']:<{w}}  expected={r['expected']:<20} actual={r['actual']:<20} "
              f"turns={r['turns']:<3} reasks={r['reasks']} ai_fields={r['fields_by_ai']}")
        if not r["pass"]:
            for t in r["transcript"][-6:]:
                print(f"      {t['speaker']:>8}: {t['text'][:160]}")
    for g in out["gate"]:
        print(f"GATE  lead {g['lead']}: allowed={g['allowed']} blocked_by={g['failed']}")
    print(f"\n{out['passed']}/{out['total']} personas behaved as expected")


if __name__ == "__main__":
    main()
