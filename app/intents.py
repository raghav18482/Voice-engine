"""Rule-based intent and signal detection. Fast, deterministic, and works with no API key.
The optional LLM layer (llm.py) can add intents these rules miss, but never removes them."""
from __future__ import annotations

import re

from .parsers import yes_no

P = lambda *xs: re.compile(r"\b(" + "|".join(xs) + r")\b", re.I)  # noqa: E731

ASK_HUMAN = P(r"real person", r"a human", r"(speak|talk) (to|with) (a |an |some)?(one|body|person|human|agent|manager|supervisor|someone|somebody)",
              r"manager", r"supervisor", r"transfer me", r"put me through", r"an operator", r"live agent", r"actual person",
              r"human being", r"proper person")
IS_AI_Q = re.compile(r"\b(are you|is this|am i talking to|am i speaking (to|with)) (a |an )?(real|robot|bot|human|ai|computer|machine|person)", re.I)
OPT_OUT = P(r"stop calling", r"don'?t call", r"do not call", r"remove (me|my number)", r"take me off", r"unsubscribe",
            r"never call", r"stop ringing", r"lose my number", r"leave me alone")
DECLINE = P(r"not interested", r"no thanks", r"no thank you", r"don'?t want (it|to|this|that|any)", r"not looking",
            r"stay where i am", r"stay with (my|them)", r"happy with (my|what)", r"not for me", r"i'?ll pass",
            r"not right now,? thanks", r"forget it", r"i'?m good,? thanks", r"not switching", r"i'?ll stay")
BUSY = P(r"busy", r"call (me )?(back|later)", r"bad time", r"driving", r"in a meeting", r"not a good time",
         r"can'?t talk", r"another time", r"at work", r"later today", r"ring (me )?back", r"not now")
ANGER_STRONG = P(r"f+u+c+k\w*", r"shit\w*", r"bullshit", r"bloody hell", r"piss(ed)? off", r"crap", r"harass\w*",
                 r"scam\w*", r"idiot\w*", r"useless", r"wtf", r"damn it", r"how dare")
ANGER_SOFT = P(r"already told", r"how many times", r"second call", r"third call", r"(two|three|four|2|3|4) of you",
               r"ridiculous", r"annoy\w*", r"sick of", r"fed up", r"waste of (my )?time", r"frustrat\w*",
               r"i'?m done", r"enough", r"again\?", r"keep calling", r"called me (twice|again)", r"seriously")
PAYMENT = P(r"credit card", r"debit card", r"my card", r"card (number|details)", r"pay (now|by|with|for it)",
            r"payment", r"bank (account|details)", r"bsb", r"direct debit", r"take (my )?money", r"pay you")
VULNERABLE = P(r"hardship", r"can'?t afford", r"struggling (to pay|with (the )?bills|financially)", r"behind on (my )?bills",
               r"disconnect\w*", r"passed away", r"died", r"funeral", r"dialysis", r"oxygen", r"domestic violence",
               r"family violence", r"carer", r"dementia", r"don'?t understand (any|what)")
DISPUTE = P(r"complain\w*", r"dispute", r"ombudsman", r"overcharg\w*", r"wrong bill", r"rip(ped)? off")
ADVICE = P(r"which (one|plan)?\s*(is|would be) (the )?(best|better|cheapest for me)", r"what do you recommend",
           r"recommend", r"should i (go|pick|choose|take|switch)", r"is it worth", r"best (plan|one|option) for me",
           r"what would you (do|pick|choose)", r"your advice", r"advise me", r"which should i")
REPEAT = P(r"sorry\??$", r"pardon", r"what was that", r"say (that|it) again", r"repeat (that|it|them)",
           r"come again", r"didn'?t (catch|hear|get) (that|it|you)", r"can you repeat", r"what did you say", r"what\?$")
WAIT = P(r"hang on", r"hold on", r"one (sec|second|moment|minute)", r"just a (sec|second|moment|minute)",
         r"give me a (sec|second|moment|minute)", r"bear with me", r"wait a (sec|second|moment)")
QUESTION_START = re.compile(r"^\s*(what|how|why|when|where|who|which|can|could|is|are|does|do|will|would|should|what'?s|how'?s)\b", re.I)


def detect(text: str, faq: list[dict] | None = None) -> dict:
    t = text.strip()
    low = t.lower()
    intents: set[str] = set()
    faq_hit = None
    for f in faq or []:
        if any(p in low for p in f["patterns"]):
            faq_hit = f["id"]
            break
    if IS_AI_Q.search(low):
        faq_hit = "is_ai"
    if ASK_HUMAN.search(low) and faq_hit != "is_ai":
        intents.add("ask_human")
    if OPT_OUT.search(low):
        intents.add("opt_out")
    if DECLINE.search(low):
        intents.add("decline")
    if BUSY.search(low):
        intents.add("busy")
    if ANGER_STRONG.search(low):
        intents.add("anger_strong")
    if ANGER_SOFT.search(low):
        intents.add("anger_soft")
    if PAYMENT.search(low):
        intents.add("payment")
    if VULNERABLE.search(low):
        intents.add("vulnerable")
    if DISPUTE.search(low):
        intents.add("dispute")
    if ADVICE.search(low):
        intents.add("advice")
    if REPEAT.search(low) and len(low.split()) <= 6:
        intents.add("repeat")
    if WAIT.search(low) and len(low.split()) <= 8:
        intents.add("wait")
    if t.endswith("?") or QUESTION_START.search(low):
        intents.add("question")
    yn = yes_no(low)
    if yn is True:
        intents.add("affirm")
    elif yn is False:
        intents.add("deny")
    return {"intents": intents, "faq": faq_hit}
