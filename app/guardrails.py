"""Guardrails that sit outside the LLM: pre-dial compliance gate and card-number redaction."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .parsers import postcode_state, words_to_digits

DATA = Path(__file__).resolve().parent.parent / "data"
STATE_TZ = {"NSW": "Australia/Sydney", "ACT": "Australia/Sydney", "VIC": "Australia/Melbourne",
            "QLD": "Australia/Brisbane", "SA": "Australia/Adelaide", "WA": "Australia/Perth",
            "TAS": "Australia/Hobart", "NT": "Australia/Darwin"}
# Stubbed permitted calling hours (local time). Confirm the exact rule with CIMET compliance.
HOURS = {0: (9, 20), 1: (9, 20), 2: (9, 20), 3: (9, 20), 4: (9, 20), 5: (9, 17), 6: None}
MAX_ATTEMPTS = 3


def _load(name: str, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def opt_outs() -> set[str]:
    return set(_load("optouts.json", []))


def add_opt_out(mobile: str):
    s = opt_outs()
    s.add(mobile)
    (DATA / "optouts.json").write_text(json.dumps(sorted(s), indent=2))


def dnc_register() -> set[str]:
    """Stub for the ACMA Do Not Call Register wash. Real version: batch wash via the register's API."""
    return set(_load("dnc_register.json", []))


def enforce_hours() -> bool:
    return os.getenv("ENFORCE_CALLING_HOURS", "false").lower() == "true"


def gate(lead: dict, now: datetime | None = None) -> dict:
    """Runs before every dial. Returns {'allowed': bool, 'checks': [...]}."""
    checks = []
    mobile = lead.get("mobile", "")
    in_dnc = mobile in dnc_register() or lead.get("dnc")
    checks.append({"check": "ACMA Do Not Call register (stub)", "ok": not in_dnc,
                   "detail": "Number is on the register" if in_dnc else "Not on register"})
    opted = mobile in opt_outs()
    checks.append({"check": "Internal opt-out list", "ok": not opted,
                   "detail": "Customer asked not to be called" if opted else "No opt-out"})
    attempts = lead.get("attempts", 0)
    checks.append({"check": f"Contact attempts (max {MAX_ATTEMPTS})", "ok": attempts < MAX_ATTEMPTS,
                   "detail": f"{attempts} previous attempts"})
    pc = (lead.get("prefilled") or {}).get("postcode")
    state = postcode_state(pc) if pc else lead.get("state", "NSW")
    tz = ZoneInfo(STATE_TZ.get(state or "NSW", "Australia/Sydney"))
    local = (now or datetime.now(tz)).astimezone(tz)
    window = HOURS[local.weekday()]
    in_hours = bool(window and window[0] <= local.hour < window[1])
    hours_ok = in_hours or not enforce_hours()
    detail = f"{local.strftime('%a %H:%M')} {state} local time"
    if not in_hours:
        detail += " is outside permitted hours" + ("" if enforce_hours() else " (enforcement off for demo)")
    checks.append({"check": "Calling hours", "ok": hours_ok, "warn": not in_hours, "detail": detail})
    consent = lead.get("contact_consent", True)
    checks.append({"check": "Callback consent from web journey", "ok": bool(consent),
                   "detail": "Customer ticked 'contact me'" if consent else "No contact consent"})
    return {"allowed": all(c["ok"] for c in checks), "checks": checks}


CARD_RE = re.compile(r"(?:\d[ -]?){13,19}")


def _luhn(num: str) -> bool:
    total, alt = 0, False
    for ch in reversed(num):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact_cards(text: str) -> tuple[str, bool]:
    """Masks anything that looks like a card number (13-19 digits, spoken or typed)."""
    norm = words_to_digits(text)
    found = False

    def sub(m):
        nonlocal found
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and (_luhn(digits) or len(digits) == 16):
            found = True
            return "[CARD NUMBER REDACTED] "
        return m.group(0)

    red = CARD_RE.sub(sub, norm)
    if found:
        # also mask 3-4 digit CVV-like tokens and expiry that follow
        red = re.sub(r"\b(cvv|cvc|security code|expiry|expires)\b[^.]*", r"\1 [REDACTED]", red, flags=re.I)
        return red, True
    return text, False
