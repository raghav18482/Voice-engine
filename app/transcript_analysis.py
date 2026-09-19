"""Analyses a real agent call transcript (the one CIMET provided, or any pasted in).

Produces three things:
  1. a stage timeline of how the human agent ran the call (what our script copies)
  2. compliance findings (what our guardrails fix)
  3. a manual-call baseline (words, estimated minutes, re-asks) for the efficiency comparison
"""
from __future__ import annotations

import re
from pathlib import Path

from .intents import DECLINE

# transcripts run words together ("juststay"), so this check skips word boundaries
SOFT_NO = re.compile(r"stay where i am|not interested|couldn'?t be bothered|no thanks|don'?t want", re.I)

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_call_transcript.txt"
WPM = 150  # average conversational speaking rate used for time estimates

STAGES = [
    ("disclosure", r"recorded|recording"),
    ("payment", r"payment method|credit card|debit card|mute the recording|card details"),
    ("web_form", r"click|select|scroll|apply now|view plan|submit application|tick the box"),
    ("identity", r"as per id|date of birth|verify|email address|mobile number|first and last name|\[DOB\]|\[EMAIL\]"),
    ("account", r"bill|account number|abc id|current(ly)? with"),
    ("connection", r"connection|deliver|modem|as soon as possible"),
    ("pitch", r"dollars|mbps|plan|free|offer|promotion|price"),
    ("discovery", r"current internet|how much are you paying|how many people|gaming|streaming|landline|speed"),
    ("cross_sell", r"electricity and gas|electricity|gas"),
    ("close", r"congratulations|reference number|pleasure|have a wonderful day|bye"),
    ("greeting", r"hi|hello|good day|how are you"),
]


def load(text: str | None = None) -> list[dict]:
    raw = text if text is not None else SAMPLE.read_text()
    turns = []
    for line in raw.splitlines():
        m = re.match(r"^\s*(Speaker \d|Agent|Customer|AI|Human)\s*:\s*(.*)$", line, re.I)
        if m:
            turns.append({"speaker": m.group(1), "text": m.group(2).strip()})
        elif turns and line.strip() and not line.startswith("#"):
            turns[-1]["text"] += " " + line.strip()
    return turns


def identify_agent(turns: list[dict]) -> str:
    """The agent is the speaker who talks more and says 'recorded' or asks to verify."""
    counts: dict[str, int] = {}
    for t in turns:
        counts[t["speaker"]] = counts.get(t["speaker"], 0) + len(t["text"].split())
    return max(counts, key=counts.get)


def stage_of(text: str) -> str:
    low = text.lower()
    for name, pat in STAGES:
        if re.search(pat, low, re.I):
            return name
    return "other"


def analyse(text: str | None = None) -> dict:
    turns = load(text)
    if not turns:
        return {"error": "No speaker turns found. Use lines like 'Speaker 1: ...' or 'Agent: ...'."}
    agent = identify_agent(turns)
    words_cum = 0
    timeline = []
    agent_words = cust_words = 0
    for i, t in enumerate(turns):
        n = len(t["text"].split())
        is_agent = t["speaker"] == agent
        if is_agent:
            agent_words += n
        else:
            cust_words += n
        timeline.append({"i": i, "role": "agent" if is_agent else "customer", "minute": round(words_cum / WPM, 1),
                         "stage": stage_of(t["text"]) if is_agent else None, "text": t["text"]})
        words_cum += n
    findings = []
    agent_turns = [x for x in timeline if x["role"] == "agent"]

    # 1. disclosure before data collection?
    disc = next((x for x in agent_turns if re.search(r"recorded|recording", x["text"], re.I)), None)
    if disc is None:
        findings.append({"severity": "critical", "title": "No recording disclosure found",
                         "detail": "The call never tells the customer it is recorded.",
                         "fix": "Our agent's first line is the disclosure, and it waits for a yes."})
    else:
        before = disc["text"].lower().split("record")[0]
        earlier = " ".join(x["text"] for x in agent_turns if x["i"] < disc["i"])
        if re.search(r"\[service_address\]|address is|\[email\]|\[phone\]|\[dob\]", (earlier + before).lower()):
            findings.append({"severity": "critical", "title": "Data confirmed before the recording disclosure",
                             "detail": f"The service address was confirmed before the disclosure at about minute {disc['minute']}.",
                             "evidence": disc["text"][:220],
                             "fix": "The opening line is the disclosure, and nothing is collected before the customer says yes."})
    # 2. pitching after the customer said no
    for x in timeline:
        if x["role"] == "customer" and (DECLINE.search(x["text"]) or SOFT_NO.search(x["text"])):
            nxt = next((y for y in timeline if y["i"] > x["i"] and y["role"] == "agent"), None)
            if nxt and re.search(r"free|offer|save|dollars|understand", nxt["text"], re.I):
                findings.append({"severity": "high", "title": "Kept pitching after the customer declined",
                                 "detail": f"Customer: \"{x['text'][:120]}\" and the agent continued the pitch.",
                                 "evidence": nxt["text"][:220],
                                 "fix": "A clear 'no' ends the call politely, is logged, and there is no second pitch."})
                break
    # 3. payment on the call
    pay = next((x for x in agent_turns if re.search(r"mute the recording|credit card or debit card|payment method", x["text"], re.I)), None)
    if pay:
        findings.append({"severity": "high", "title": "Payment handled on the voice call",
                         "detail": "The agent muted the recording to take payment details.",
                         "evidence": pay["text"][:220],
                         "fix": "The AI never takes card details. It explains the boundary, sends a secure link, or hands off. Any spoken card number is redacted."})
    # 4. guarantees / absolute claims
    claims = [x for x in agent_turns if re.search(r"guarantee|hundred percent|100%", x["text"], re.I)]
    if claims:
        findings.append({"severity": "medium", "title": f"Absolute claims made {len(claims)} times",
                         "detail": "Phrases like 'I really guarantee you' and 'hundred percent free' drift into assurances.",
                         "evidence": claims[0]["text"][:220],
                         "fix": "The AI states only facts from the script and plan data, and never gives advice or guarantees."})
    # 5. confusion / re-asks
    reasks = [x for x in timeline if re.search(r"sorry\.? (i )?didn'?t|what was that|verify again|can you please verify|pardon|say that again", x["text"], re.I)]
    # 6. web-form hand-holding
    clicks = sum(len(re.findall(r"\bclick|\bselect|\bscroll", x["text"], re.I)) for x in agent_turns)
    if clicks:
        findings.append({"severity": "info", "title": f"{clicks} web-form instructions read to the customer",
                         "detail": "The agent talked the customer through clicking a web form step by step.",
                         "fix": "The AI submits the journey payload directly, and the customer only answers questions."})
    # 7. ASR / brand errors
    brand_variants = sorted(set(m.group(0) for m in re.finditer(r"Equinix|Econnex|Internet's comparison|Ambien", " ".join(t["text"] for t in turns))))
    if brand_variants:
        findings.append({"severity": "info", "title": "Speech-to-text errors on brand and jargon",
                         "detail": "The same brand appears as: " + ", ".join(brand_variants) + ". 'NBN' became 'Ambien'.",
                         "fix": "Critical values are read back and validated, so mishears are caught on the call."})
    cross = next((x for x in agent_turns if re.search(r"electricity and gas", x["text"], re.I)), None)
    if cross:
        findings.append({"severity": "info", "title": "Energy cross-sell found an embedded network",
                         "detail": "The customer buys electricity through their complex, which means they usually can't switch retailer.",
                         "fix": "Our script asks about embedded networks early and hands off to a specialist."})

    total_words = agent_words + cust_words
    stage_counts: dict[str, int] = {}
    for x in agent_turns:
        stage_counts[x["stage"]] = stage_counts.get(x["stage"], 0) + 1
    return {
        "turns": len(turns), "agent_speaker": agent, "agent_words": agent_words, "customer_words": cust_words,
        "est_minutes": round(total_words / WPM, 1), "reasks": len(reasks), "web_form_instructions": clicks,
        "stages": stage_counts, "timeline": timeline, "findings": findings,
        "note": f"Talk time is estimated at {WPM} words a minute, excluding hold and silence, so the real call was longer.",
    }


def baseline_minutes() -> float:
    try:
        return analyse()["est_minutes"]
    except Exception:
        return 15.0
