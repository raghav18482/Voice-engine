"""Optional LLM layer. With no API key the agent runs fully on rules.

With OPENAI_API_KEY or ANTHROPIC_API_KEY set, each customer turn is also sent to a small,
fast model that returns structured JSON: field values WITH the exact words that support
them, intents, sentiment and confidence. The engine only accepts a value if its evidence
appears in what the customer actually said, and the value still passes the same
deterministic validators — so the model can help understand speech but can't invent data.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

log = logging.getLogger("llm")
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "4"))

SYSTEM = """You analyse one customer utterance from an Australian energy sales call.
Return ONLY a JSON object:
{"values": {"<field_key>": {"value": "<normalised value>", "evidence": "<exact words copied from the utterance>"}},
 "intents": [zero or more of: "affirm","deny","ask_human","decline","opt_out","busy","advice","payment","question","anger","vulnerable"],
 "sentiment": <-1.0 to 1.0>,
 "confidence": <0.0 to 1.0, how sure you are you understood>}
Rules: only extract values the customer actually stated. Evidence must be copied verbatim from the utterance.
Never guess. Dates as spoken (the system parses them). Emails: rebuild from spelling ("j dot smith at gmail dot com" -> "j.smith@gmail.com")."""


def provider() -> Optional[str]:
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _user_prompt(utterance: str, agent_question: str, fields: list[dict]) -> str:
    fl = "\n".join(f"- {f['key']} ({f['type']}): {f.get('label', '')}"
                   + (f" options={list(f['options'].keys())}" if f.get("options") else "") for f in fields)
    return f"Agent just asked: \"{agent_question}\"\nFields still needed (current one first):\n{fl}\n\nCustomer said: \"{utterance}\""


async def analyse(utterance: str, agent_question: str, fields: list[dict]) -> Optional[dict]:
    p = provider()
    if not p:
        return None
    prompt = _user_prompt(utterance, agent_question, fields)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if p == "openai":
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                    json={"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "temperature": 0,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]
            else:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                    json={"model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"), "max_tokens": 400,
                          "system": SYSTEM, "messages": [{"role": "user", "content": prompt}]})
                r.raise_for_status()
                text = "".join(b.get("text", "") for b in r.json()["content"])
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:  # never let the LLM break a live call
        log.warning("LLM call failed, falling back to rules: %s", e)
        return None


def evidence_ok(evidence: str, utterance: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9@.]", "", s.lower())  # noqa: E731
    e = norm(evidence or "")
    return len(e) >= 2 and e in norm(utterance)
