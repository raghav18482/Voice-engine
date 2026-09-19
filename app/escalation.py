"""Escalation engine: decides, every turn, whether the AI should hand the call to a human.

Immediate triggers transfer at once. Softer signals add to a score that decays on clean
turns; crossing the threshold transfers. All thresholds are here so they can be tuned
after running the persona harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

THRESHOLD = 4
LOW_CONF = 0.55          # browser ASR confidence below this counts as a low-confidence turn
LOW_CONF_STREAK = 2
MAX_FIELD_FAILURES = 3   # third failed capture of the same field -> handoff (offer at 2)


@dataclass
class Decision:
    signal: str          # asks | anger | confusion | off_script | sensitive | low_conf | soft
    reason: str


@dataclass
class Escalation:
    score: int = 0
    anger_seen: bool = False
    advice_count: int = 0
    payment_count: int = 0
    offscript_count: int = 0
    low_conf_streak: int = 0
    events: list = field(default_factory=list)

    def _log(self, turn: int, signal: str, delta: int, note: str):
        self.events.append({"turn": turn, "signal": signal, "delta": delta, "score": self.score, "note": note})

    def assess(self, turn: int, intents: set, confidence: Optional[float], sentiment: Optional[float]) -> Optional[Decision]:
        if "ask_human" in intents:
            self._log(turn, "asks", 0, "Customer asked for a person")
            return Decision("asks", "Customer asked to speak to a person")
        if "anger_strong" in intents:
            self.anger_seen = True
            self._log(turn, "anger", 0, "Strong frustration or profanity")
            return Decision("anger", "Strong frustration or profanity")
        if "vulnerable" in intents:
            self._log(turn, "sensitive", 0, "Possible vulnerable customer or hardship")
            return Decision("sensitive", "Possible vulnerable customer or financial hardship")
        if "dispute" in intents:
            self._log(turn, "sensitive", 0, "Complaint or dispute")
            return Decision("sensitive", "Customer raised a complaint or dispute")
        if "anger_soft" in intents:
            self.anger_seen = True
            self.score += 2
            self._log(turn, "anger", 2, "Frustration signals")
        if "opt_out" in intents and self.anger_seen:
            self._log(turn, "anger", 0, "Frustrated and asked us to stop calling")
            return Decision("anger", "Frustrated customer asked us to stop calling")
        if "payment" in intents:
            self.payment_count += 1
            self._log(turn, "sensitive", 0, f"Payment raised ({self.payment_count})")
            if self.payment_count >= 2:
                return Decision("sensitive", "Customer wants to pay now; card details can't be taken by AI voice")
        if "advice" in intents:
            self.advice_count += 1
            self.score += 2
            self._log(turn, "off_script", 2, "Asked for advice")
            if self.advice_count >= 2:
                return Decision("off_script", "Customer keeps asking for advice the AI can't give")
        if sentiment is not None and sentiment <= -0.5:
            self.score += 2
            self._log(turn, "anger", 2, f"Negative sentiment {sentiment:+.1f}")
        if confidence is not None and 0 < confidence < LOW_CONF:
            self.low_conf_streak += 1
            self.score += 1
            self._log(turn, "low_conf", 1, f"Low ASR confidence {confidence:.2f}")
            if self.low_conf_streak >= LOW_CONF_STREAK:
                return Decision("low_conf", "Speech recognition unsure on consecutive turns")
        else:
            self.low_conf_streak = 0
        negatives = intents & {"anger_soft", "advice", "payment"}
        if not negatives and self.score > 0 and not (confidence is not None and 0 < confidence < LOW_CONF):
            self.score -= 1
        if self.score >= THRESHOLD:
            self._log(turn, "soft", 0, "Accumulated signals crossed threshold")
            return Decision("soft", "Several frustration or uncertainty signals in a row")
        return None

    def off_script(self, turn: int) -> Optional[Decision]:
        self.offscript_count += 1
        self.score += 2
        self._log(turn, "off_script", 2, "Question outside the script")
        if self.offscript_count >= 2 or self.score >= THRESHOLD:
            return Decision("off_script", "Questions the script doesn't cover")
        return None
