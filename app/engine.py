"""The conversation engine: a script-driven state machine.

The YAML script decides WHAT is asked and in which order. The engine decides WHEN to move
on (validated + read back), and the escalation engine decides WHEN to step aside.
The LLM (optional) only helps understand what the customer said.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import llm, sandbox
from .escalation import MAX_FIELD_FAILURES, Escalation
from .guardrails import add_opt_out, redact_cards
from .intents import detect
from .parsers import Parsed, parse, postcode_state, spell_digits

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "data"


def load_script(name: str = "energy") -> dict:
    return yaml.safe_load((ROOT / "scripts" / f"{name}.yaml").read_text())


def load_plans() -> list[dict]:
    return json.loads((DATA / "plans.json").read_text())


FUEL_LABEL = {"electricity": "electricity", "gas": "gas", "both": "electricity and gas"}


def _n(x) -> str:
    return f"{x:g}"


def plan_short(p: dict) -> str:
    parts = []
    if p.get("elec_c_kwh") is not None:
        parts.append(f"electricity {_n(p['elec_c_kwh'])} cents per kilowatt hour")
    if p.get("gas_c_mj") is not None:
        parts.append(f"gas {_n(p['gas_c_mj'])} cents per megajoule")
    return " and ".join(parts)


def plan_sentence(p: dict) -> str:
    parts = []
    if p.get("elec_c_kwh") is not None:
        parts.append(f"electricity usage {_n(p['elec_c_kwh'])} cents per kilowatt hour with a supply charge of {_n(p['supply_c_day'])} cents a day")
    if p.get("gas_c_mj") is not None:
        parts.append(f"gas usage {_n(p['gas_c_mj'])} cents per megajoule with a supply charge of {_n(p['gas_supply_c_day'])} cents a day")
    return " and ".join(parts)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallSession:
    def __init__(self, lead: dict, script: Optional[dict] = None, *, use_llm: bool = True,
                 simulated: bool = False, today: Optional[date] = None):
        self.id = uuid.uuid4().hex[:8]
        self.lead = copy.deepcopy(lead)
        self.script = script or load_script()
        self.use_llm = use_llm and llm.provider() is not None
        self.simulated = simulated
        self.today = today or date.today()
        self.phase = "init"
        self.fields: dict[str, dict] = {}
        self.skipped: set[str] = set()
        self.partial: dict[str, Any] = {}
        self.retries: dict[str, int] = defaultdict(int)
        self.current: Optional[str] = None
        self.transcript: list[dict] = []
        self.outbox: list[dict] = []
        self.escalation = Escalation()
        self.handoff: Optional[dict] = None
        self.outcome: Optional[str] = None
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.last_prompt = ""
        self.plan_options: list[dict] = []
        self.consent_at: Optional[str] = None
        self.events: list[dict] = []
        self.human_agent: Optional[str] = None
        self.handoff_at: Optional[float] = None
        self.accepted_at: Optional[float] = None
        self.reasks = 0
        self.submission: Optional[dict] = None
        self.callback_when: Optional[str] = None
        self.announced: set[str] = set()
        self.flags: list[str] = []
        self.hold_said = False
        self.eic_at: Optional[str] = None
        self.phase_before_handoff: Optional[str] = None
        self.llm_calls = 0
        self._specs = {f["key"]: (s, f) for s in self.script["sections"] for f in s.get("fields", [])}
        for k, v in (self.lead.get("prefilled") or {}).items():
            self._store(k, v, source="web", confirmed=True)
        # Contact details from the web form are re-confirmed by readback, not re-asked.
        for k in ("email", "mobile"):
            if self.lead.get(k):
                p = parse(k, self.lead[k])
                if p.ok:
                    self.fields[k] = {"value": p.value, "display": p.display, "spoken": p.spoken,
                                      "source": "web", "confirmed": False, "at": None}

    # ------------------------------------------------------------ helpers
    def fmt(self, key: str, **kw) -> str:
        base = {"first_name": self.lead.get("first_name", "there"), "agent_name": self.script["agent_name"],
                "brand": self.script["brand"]}
        base.update(kw)
        return self.script["lines"][key].format(**base)

    def say(self, text: str, speaker: str = "ai", prompt: bool = False):
        text = " ".join(text.split())
        self.transcript.append({"speaker": speaker, "text": text, "t": round(time.time() - self.started_at, 1)})
        self.outbox.append({"type": "say", "speaker": speaker, "text": text})
        if prompt:
            self.last_prompt = text

    def log(self, kind: str, detail: str = ""):
        self.events.append({"t": round(time.time() - self.started_at, 1), "kind": kind, "detail": detail})
        self.outbox.append({"type": "event", "kind": kind, "detail": detail})

    def drain(self) -> list[dict]:
        out, self.outbox = self.outbox, []
        return out

    def spec(self, key: str) -> dict:
        return self._specs[key][1]

    def _store(self, key: str, value: Any, source: str, confirmed: bool, parsed: Optional[Parsed] = None):
        if key == "full_name" and isinstance(value, dict):
            disp = f"{value['first_name']} {value['last_name']}"
        else:
            disp = parsed.display if parsed else str(value)
        spoken = parsed.spoken if parsed else disp
        if key in ("postcode",) and not parsed:
            spoken = spell_digits(str(value))
        self.fields[key] = {"value": value, "display": disp, "spoken": spoken, "source": source,
                            "confirmed": confirmed, "at": round(time.time() - self.started_at, 1)}
        if parsed and parsed.extra.get("state"):
            self.fields["state"] = {"value": parsed.extra["state"], "display": parsed.extra["state"], "spoken": "",
                                    "source": source, "confirmed": True, "at": None}
        if key == "postcode" and "state" not in self.fields:
            st = postcode_state(str(value))
            if st:
                self.fields["state"] = {"value": st, "display": st, "spoken": "", "source": source,
                                        "confirmed": True, "at": None}

    def val(self, key: str) -> Any:
        f = self.fields.get(key)
        return f["value"] if f else None

    def _condition_ok(self, fspec: dict) -> bool:
        c = fspec.get("condition")
        return not c or self.val(c["field"]) == c["equals"]

    def ctx(self, key: Optional[str] = None) -> dict:
        return {"today": self.today, "plan_options": self.plan_options, "postcode": self.val("postcode"),
                "partial": self.partial.get(key) if key else None}

    def required_fields(self) -> list[tuple[dict, dict]]:
        out = []
        for s in self.script["sections"]:
            for f in s.get("fields", []):
                if self._condition_ok(f):
                    out.append((s, f))
        return out

    def missing(self) -> list[str]:
        return [f["label"] for s, f in self.required_fields()
                if not (f["key"] in self.fields and (self.fields[f["key"]]["confirmed"] or not f.get("critical")))
                and f["key"] not in self.skipped]

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.phase = "consent"
        self.log("call_started", f"Lead {self.lead['id']}")
        self.say(self.fmt("opening") + " " + self.fmt("consent"), prompt=True)
        self.log("disclosure_read", "Recording disclosure spoken before any question (verbatim)")
        return self.drain()

    def end(self, outcome: str, line: Optional[str] = None):
        if line:
            self.say(line)
        self.phase = "ended"
        self.outcome = outcome
        self.ended_at = time.time()
        self.log("call_ended", outcome)
        self.outbox.append({"type": "ended", "outcome": outcome})

    # ------------------------------------------------------------ main turn handler
    async def handle(self, raw_text: str, confidence: Optional[float] = None) -> list[dict]:
        if self.phase in ("ended", "init"):
            return self.drain()
        text, had_card = redact_cards(raw_text or "")
        text = text.strip()
        turn_no = len(self.transcript)
        self.transcript.append({"speaker": "customer", "text": text, "t": round(time.time() - self.started_at, 1),
                                "conf": confidence})
        if not text:
            return self.drain()
        if had_card:
            self.log("card_redacted", "Card number spoken: masked in transcript, never stored")

        if self.phase == "human":
            return self.drain()  # the human agent is talking; console sees the transcript
        if self.phase == "handoff_pending":
            if not self.hold_said:
                self.hold_said = True
                self.say(self.fmt("handoff_hold"))
            return self.drain()

        d = detect(text, self.script.get("faq"))
        intents: set = d["intents"]
        sentiment = None
        llm_res = None
        if self.use_llm and self.phase not in ("consent",):
            llm_res = await llm.analyse(text, self.last_prompt, self._llm_fields())
            self.llm_calls += 1
            if llm_res:
                sentiment = llm_res.get("sentiment")
                extra = {"ask_human", "decline", "opt_out", "busy", "advice", "payment"} & set(llm_res.get("intents") or [])
                # the model can add intents, but can't override a clear yes/no from rules
                intents |= extra
                if "anger" in (llm_res.get("intents") or []):
                    intents.add("anger_soft")
                if "vulnerable" in (llm_res.get("intents") or []):
                    intents.add("vulnerable")
                if (llm_res.get("confidence") is not None and llm_res["confidence"] < 0.4
                        and confidence is None):
                    confidence = 0.5
        self.transcript[-1]["intents"] = sorted(intents)

        if had_card:
            self.do_handoff("sensitive", "Card details were spoken; AI voice never takes payment", quote="[card number redacted]")
            return self.drain()

        # consent is binary and comes first: nothing else is processed before it
        if self.phase == "consent":
            self._consent(intents)
            return self.drain()

        dec = self.escalation.assess(turn_no, intents, confidence, sentiment)
        if dec:
            self.do_handoff(dec.signal, dec.reason, quote=text)
            return self.drain()

        words = len(text.split())
        if "wait" in intents and words <= 8:
            self.say(self.fmt("wait"))
            return self.drain()
        if "repeat" in intents and words <= 6:
            self.say(self.last_prompt or self.fmt("intro_reask"))
            return self.drain()
        if "opt_out" in intents:
            add_opt_out(self.lead.get("mobile", ""))
            self.log("opt_out", "Number added to internal do-not-call list")
            self.end("opt_out", self.fmt("opt_out"))
            return self.drain()
        if "decline" in intents and self.phase not in ("readback",):
            self.log("declined", "Customer declined; no further pitch (respect no)")
            self.end("declined", self.fmt("decline"))
            return self.drain()
        if "busy" in intents and self.phase != "callback":
            self.phase = "callback"
            self.say(self.fmt("busy"), prompt=True)
            return self.drain()
        if "payment" in intents:
            self.log("payment_boundary", "Explained AI can't take card details")
            self.say(self.fmt("payment_boundary") + " " + self.last_prompt)
            return self.drain()
        if d["faq"]:
            faq = next(f for f in self.script["faq"] if f["id"] == d["faq"])
            self.say(faq["answer"].format(agent_name=self.script["agent_name"], brand=self.script["brand"])
                     + " " + self.last_prompt)
            return self.drain()
        if "advice" in intents:
            self.log("no_advice", "Declined to give advice")
            self.say(self.fmt("no_advice") + " " + self.last_prompt)
            return self.drain()

        handler = {"intro": self._intro, "collect": self._collect, "readback": self._readback,
                   "offer_human": self._offer_human, "plan": self._plan, "eic": self._eic,
                   "eic_declined": self._eic_declined, "callback": self._callback}.get(self.phase)
        if handler:
            r = handler(text, intents, llm_res)
            if hasattr(r, "__await__"):
                await r
        return self.drain()

    def _llm_fields(self) -> list[dict]:
        keys = []
        if self.current:
            keys.append(self.current)
        for s, f in self.required_fields():
            if f["key"] not in self.fields and f["key"] not in keys and f["type"] not in ("plan",):
                keys.append(f["key"])
        return [self.spec(k) for k in keys[:6] if k in self._specs]

    # ------------------------------------------------------------ phases
    def _consent(self, intents: set):
        if "affirm" in intents and "deny" not in intents:
            self.consent_at = now_iso()
            self.log("consent_given", "Customer agreed to recording")
            self.phase = "intro"
            self.say(self.fmt("intro"), prompt=True)
        elif "deny" in intents or "decline" in intents or "opt_out" in intents:
            self.log("consent_refused", "No recording consent; nothing collected")
            self.end("no_consent", self.fmt("no_consent"))
        else:
            self.retries["_consent"] += 1
            if self.retries["_consent"] >= 3:
                self.end("no_consent", self.fmt("no_consent"))
            else:
                self.say(self.fmt("consent_reask"), prompt=True)

    def _intro(self, text, intents, llm_res):
        if "affirm" in intents:
            self.phase = "collect"
            prefix = self.fmt("resume") if self.lead.get("prefilled") else "Great."
            self.ask_next(prefix)
        elif "deny" in intents:
            self.phase = "callback"
            self.say(self.fmt("busy"), prompt=True)
        else:
            self.retries["_intro"] += 1
            if self.retries["_intro"] >= 2:
                self.phase = "collect"
                self.ask_next("Let's get started.")
            else:
                self.say(self.fmt("intro_reask"), prompt=True)

    def ask_next(self, prefix: str = ""):
        for s in self.script["sections"]:
            stype = s.get("type")
            if stype == "plan":
                if "selected_plan" not in self.fields:
                    self._present_plans(prefix)
                    return
                continue
            if stype == "eic":
                self._start_eic(prefix)
                return
            for f in s.get("fields", []):
                k = f["key"]
                if not self._condition_ok(f) or k in self.skipped:
                    continue
                cur = self.fields.get(k)
                if cur and (cur["confirmed"] or not f.get("critical")):
                    continue
                intro = ""
                if s["id"] not in self.announced and s.get("intro") and not cur:
                    intro = s["intro"]
                self.announced.add(s["id"])
                self.current = k
                if cur and f.get("critical"):
                    self.phase = "readback"
                    line = (f.get("ask_prefilled") if cur["source"] == "web" and f.get("ask_prefilled") else f["readback"])
                    self.say(f"{prefix} {intro} {line.format(spoken=cur['spoken'])}", prompt=True)
                    return
                self.phase = "collect"
                self.say(f"{prefix} {intro} {f['ask']}", prompt=True)
                return

    async def _collect(self, text, intents, llm_res):
        k = self.current
        f = self.spec(k)
        p = parse(f["type"], text, f, self.ctx(k))
        source = "ai"
        if not p.ok and not p.skip and llm_res:
            lv = (llm_res.get("values") or {}).get(k)
            if lv and llm.evidence_ok(lv.get("evidence", ""), text):
                p2 = parse(f["type"], str(lv.get("value", "")), f, self.ctx(k))
                if p2.ok:
                    p, source = p2, "ai+llm"
        # opportunistic: other fields the customer volunteered (LLM only, still validated)
        if llm_res:
            for ok_key, lv in (llm_res.get("values") or {}).items():
                if ok_key == k or ok_key not in self._specs or ok_key in self.fields:
                    continue
                fs = self.spec(ok_key)
                if fs["type"] == "plan" or not llm.evidence_ok((lv or {}).get("evidence", ""), text):
                    continue
                p3 = parse(fs["type"], str(lv.get("value", "")), fs, self.ctx(ok_key))
                if p3.ok:
                    self._accept(ok_key, fs, p3, "ai+llm", announce=False)
                    self.log("captured_early", f"{fs['label']} captured before being asked")
        if p.skip and f.get("optional"):
            self.skipped.add(k)
            self.ask_next("No problem.")
            return
        if p.ok:
            self.partial.pop(k, None)
            if self._accept(k, f, p, source):
                return
            if f.get("critical"):
                self.phase = "readback"
                self.say(f["readback"].format(spoken=p.spoken), prompt=True)
            else:
                self.ask_next("Got it.")
            return
        if p.extra.get("partial"):
            self.partial[k] = p.extra["partial"]
            self.say(p.error, prompt=True)
            return
        if "question" in intents:
            dec = self.escalation.off_script(len(self.transcript))
            if dec:
                self.do_handoff(dec.signal, dec.reason, quote=text)
                return
            self.log("off_script", text)
            self.say(self.fmt("off_script") + " " + f["ask"], prompt=True)
            return
        self._failure(k, f, p.error)

    def _accept(self, k: str, f: dict, p: Parsed, source: str, announce: bool = True) -> bool:
        """Stores a value. Returns True if the value triggered an action that took over the call."""
        self._store(k, p.value, source, confirmed=not f.get("critical"), parsed=p)
        if p.extra.get("typo_fixed"):
            self.log("typo_corrected", f"{f['label']}: corrected a likely typo to {p.display}")
        if f.get("flag_if") and str(p.value) == f["flag_if"]:
            self.flags.append(k)
            self.log("flag", f"{f['label']} = {p.value}: flagged for retailer registration")
        act = (f.get("on_value") or {}).get(str(p.value))
        if act and act.get("action") == "handoff" and announce:
            self.do_handoff(act["signal"], act["reason"], quote=self.transcript[-1]["text"])
            return True
        if k == "postcode" and self.fields.get("state", {}).get("value") == "WA" and announce:
            self.fields[k]["confirmed"] = True
            self.log("ineligible", "WA households can't choose electricity retailer")
            self.end("ineligible", self.fmt("ineligible_wa"))
            return True
        return False

    def _failure(self, k: str, f: dict, hint: str = ""):
        self.retries[k] += 1
        self.reasks += 1
        n = self.retries[k]
        self.log("capture_failed", f"{f['label']} attempt {n}")
        if n >= MAX_FIELD_FAILURES:
            self.do_handoff("confusion", f"Couldn't capture {f['label']} after {n} attempts",
                            quote=self.transcript[-1]["text"])
        elif n == MAX_FIELD_FAILURES - 1:
            self.phase = "offer_human"
            self.say(self.fmt("offer_human"), prompt=True)
        else:
            self.phase = "collect"
            self.say(f"Sorry, I didn't quite get that. {hint} {f['reask']}", prompt=True)

    def _readback(self, text, intents, llm_res):
        k = self.current
        f = self.spec(k)
        if "affirm" in intents and "deny" not in intents:
            self.fields[k]["confirmed"] = True
            self.ask_next("Thanks.")
            return
        # a correction may carry the new value: "no, it's gmail not gmial"
        p = parse(f["type"], text, f, self.ctx(k))
        if p.ok and p.value != self.fields[k]["value"]:
            self._store(k, p.value, "ai", confirmed=False, parsed=p)
            self.log("corrected", f"{f['label']} corrected by customer")
            self.say(f["readback"].format(spoken=p.spoken), prompt=True)
            return
        if "deny" in intents:
            del self.fields[k]
            self.log("readback_rejected", f["label"])
            self.retries[k] += 1
            self.reasks += 1
            if self.retries[k] >= MAX_FIELD_FAILURES:
                self.do_handoff("confusion", f"{f['label']} rejected on readback {self.retries[k]} times",
                                quote=text)
            elif self.retries[k] == MAX_FIELD_FAILURES - 1:
                self.phase = "offer_human"
                self.say(self.fmt("offer_human"), prompt=True)
            else:
                self.phase = "collect"
                self.say(f"Sorry about that. {f['reask']}", prompt=True)
            return
        self.say("Sorry, is that right? A yes or no is fine.", prompt=False)

    async def _offer_human(self, text, intents, llm_res):
        if "affirm" in intents or "ask_human" in intents:
            self.do_handoff("confusion", f"Customer accepted a transfer after trouble with {self.spec(self.current)['label']}",
                            quote=text)
            return
        f = self.spec(self.current)
        p = parse(f["type"], text, f, self.ctx(self.current))
        if p.ok:
            self.phase = "collect"
            await self._collect(text, intents, llm_res)
            return
        self.phase = "collect"
        self.say(f"{self.fmt('try_again')} {f['reask']}", prompt=True)

    # ------------------------------------------------------------ plan choice
    def _present_plans(self, prefix: str = ""):
        fuel = self.val("fuel_type") or "electricity"
        state = self.val("state") or "NSW"
        opts = [p for p in load_plans() if p["fuel"] == fuel and state in p["states"]]
        opts.sort(key=lambda p: -p["pct_below_reference"])
        self.plan_options = opts[:3]
        self.current = "selected_plan"
        if not self.plan_options:
            self.do_handoff("off_script", f"No {fuel} plans in the catalogue for {state}")
            return
        self.phase = "plan"
        lines = [self.fmt("plan_intro", fuel_label=FUEL_LABEL[fuel])]
        for i, p in enumerate(self.plan_options, 1):
            lines.append(f"Option {i}: {p['retailer']} {p['name']}, {plan_short(p)}, "
                         f"{p['pct_below_reference']} percent below the reference price.")
        lines.append(self.fmt("plan_ask"))
        self.log("plans_presented", ", ".join(p["id"] for p in self.plan_options))
        self.say(f"{prefix} " + " ".join(lines), prompt=True)

    def _plan(self, text, intents, llm_res):
        f = self.spec("selected_plan")
        p = parse("plan", text, f, self.ctx())
        if p.ok:
            self._store("selected_plan", p.value, "ai", confirmed=True, parsed=p)
            self.ask_next(f"Okay, {p.display}.")
            return
        if "question" in intents:
            dec = self.escalation.off_script(len(self.transcript))
            if dec:
                self.do_handoff(dec.signal, dec.reason, quote=text)
                return
            self.say(self.fmt("off_script") + " " + self.fmt("plan_ask"))
            return
        self.retries["selected_plan"] += 1
        self.reasks += 1
        if self.retries["selected_plan"] >= MAX_FIELD_FAILURES:
            self.do_handoff("confusion", "Couldn't confirm a plan choice", quote=text)
        else:
            self.say("Sorry, which option was that? " + self.fmt("plan_ask"))

    def selected_plan(self) -> Optional[dict]:
        pid = self.val("selected_plan")
        for p in self.plan_options or load_plans():
            if p["id"] == pid:
                return p
        return next((p for p in load_plans() if p["id"] == pid), None)

    # ------------------------------------------------------------ explicit informed consent
    def _eic_text(self) -> str:
        p = self.selected_plan()
        rates = plan_sentence(p).capitalize() + "." if p else ""
        return self.fmt("eic", fuel_label=FUEL_LABEL.get(self.val("fuel_type"), "energy"),
                        service_address=self.fields.get("service_address", {}).get("display", "your address"),
                        plan_name=p["name"] if p else "", retailer=p["retailer"] if p else "", rates_sentence=rates)

    def _start_eic(self, prefix: str = ""):
        self.phase = "eic"
        self.current = None
        self.say(f"{prefix} {self._eic_text()}", prompt=True)
        self.log("eic_read", "Explicit informed consent read verbatim")

    async def _eic(self, text, intents, llm_res):
        if "affirm" in intents and "deny" not in intents:
            self._store("eic_accepted", True, "customer", confirmed=True)
            self.eic_at = now_iso()
            self.log("eic_accepted", text)
            await self.submit(agent="ai")
        elif "deny" in intents:
            self.log("eic_declined", text)
            self.phase = "eic_declined"
            self.say(self.fmt("eic_declined"), prompt=True)
        else:
            self.retries["_eic"] += 1
            if self.retries["_eic"] >= 3:
                self.do_handoff("low_conf", "No clear yes or no to explicit informed consent", quote=text)
            else:
                self.say(self.fmt("eic_reask"), prompt=True)

    def _eic_declined(self, text, intents, llm_res):
        if "affirm" in intents or "ask_human" in intents:
            self.do_handoff("asks", "Declined consent but wants to talk it through", quote=text)
        else:
            self.end("declined_at_consent", self.fmt("eic_declined_end"))

    def _callback(self, text, intents, llm_res):
        self.callback_when = text
        self.log("callback_booked", text)
        self.end("callback", self.fmt("callback_booked", when=text))

    # ------------------------------------------------------------ submit
    def build_payload(self, agent: str = "ai") -> dict:
        v = self.val
        name = v("full_name") or {}
        p = self.selected_plan() or {}
        yn = lambda k: v(k) == "yes"  # noqa: E731
        return {
            "lead_id": self.lead["id"],
            "vertical": "energy",
            "channel": "ai_voice" if agent == "ai" else "ai_voice_with_human",
            "call_id": self.id,
            "customer": {"title": name.get("title"), "first_name": name.get("first_name", ""),
                         "last_name": name.get("last_name", ""), "dob": v("dob") or "",
                         "email": v("email") or "", "mobile": v("mobile") or ""},
            "property": {"property_type": v("property_type"), "move_type": v("move_in_or_switch"),
                         "postcode": v("postcode"), "state": v("state"),
                         "service_address": v("service_address") or "",
                         "move_in_date": v("move_in_date"), "embedded_network": yn("embedded_network")},
            "energy": {"fuel_type": v("fuel_type"), "current_retailer": v("current_retailer"),
                       "solar": yn("solar"), "life_support": yn("life_support"), "concession": yn("concession"),
                       "last_bill_amount": v("bill_amount")},
            "plan": {"plan_id": p.get("id", ""), "retailer": p.get("retailer", ""), "name": p.get("name", "")},
            "consent": {"recording_disclosed_at": self.consent_at or "", "eic_accepted": bool(v("eic_accepted")),
                        "eic_accepted_at": self.eic_at or "",
                        "script_version": self.script["version"]},
            "flags": [f"{k}_registration_required" for k in self.flags],
        }

    async def submit(self, agent: str = "ai") -> dict:
        payload = self.build_payload(agent)
        res = await sandbox.submit(payload)
        self.submission = {"payload": payload, "result": res, "agent": agent, "at": now_iso()}
        if res.get("ok"):
            self.log("submitted", f"Reference {res.get('reference')}")
            ref = res.get("reference") or ""
            letters, _, digits = ref.partition("-")
            ref_spoken = f"{' '.join(letters)}, {' '.join(digits)}" if ref else ""
            p = self.selected_plan() or {"retailer": "Your retailer"}
            line = self.fmt("submitted", ref_spoken=ref_spoken, retailer=p["retailer"])
            if agent == "ai":
                self.end("submitted", line)
            else:
                self.outcome = "submitted_by_human"
                self.outbox.append({"type": "submitted", "reference": ref})
        else:
            self.log("submit_failed", "; ".join(res.get("errors", [])))
            if agent == "ai":
                self.do_handoff("off_script", "Journey sandbox rejected the payload: " + "; ".join(res.get("errors", []))[:200])
        return res

    # ------------------------------------------------------------ handoff
    def do_handoff(self, signal: str, reason: str, quote: Optional[str] = None):
        if self.phase in ("handoff_pending", "human", "ended"):
            return
        self.phase_before_handoff = self.phase
        self.phase = "handoff_pending"
        self.handoff_at = time.time()
        self.handoff = {"signal": signal, "reason": reason, "quote": quote,
                        "at": round(self.handoff_at - self.started_at, 1)}
        self.say(self.fmt("handoff"))
        self.log("handoff_requested", f"{signal}: {reason}")
        self.outbox.append({"type": "handoff", "signal": signal, "reason": reason})
        if self.simulated:
            self.outcome = f"handoff:{signal}"

    def accept_handoff(self, agent_name: str):
        if self.phase != "handoff_pending":
            return self.drain()
        self.phase = "human"
        self.human_agent = agent_name
        self.accepted_at = time.time()
        self.log("handoff_accepted", agent_name)
        self.say(self.fmt("human_greeting", agent=agent_name), speaker="human")
        return self.drain()

    def handoff_timeout(self):
        if self.phase == "handoff_pending":
            self.log("handoff_timeout", "No human answered; priority callback booked")
            self.end("callback_priority", self.fmt("handoff_timeout"))
        return self.drain()

    def human_say(self, text: str):
        self.say(text, speaker="human")
        return self.drain()

    def human_set_field(self, key: str, value: str):
        if key not in self._specs:
            return self.drain()
        f = self.spec(key)
        p = parse(f["type"], value, f, self.ctx(key))
        if p.ok:
            self._store(key, p.value, "human", confirmed=True, parsed=p)
        else:
            self._store(key, value, "human", confirmed=True)
        self.log("human_edit", f"{f['label']} set by {self.human_agent or 'agent'}")
        return self.drain()

    # ------------------------------------------------------------ views
    def card(self) -> dict:
        captured = []
        for s, f in self.required_fields():
            cur = self.fields.get(f["key"])
            if cur:
                captured.append({"key": f["key"], "label": f["label"], "value": cur["display"],
                                 "confirmed": cur["confirmed"], "source": cur["source"], "section": s["title"]})
        phase = self.phase_before_handoff or self.phase
        friendly = {"consent": "Recording consent", "intro": "Intro, before any questions", "plan": "Plan choice",
                    "eic": "Explicit informed consent", "eic_declined": "Declined at consent",
                    "callback": "Booking a callback"}
        section = friendly.get(phase) or next((s["title"] for s, f in self.required_fields() if f["key"] == self.current), None)
        return {
            "customer": self.lead.get("first_name"), "lead_id": self.lead["id"], "mobile": self.lead.get("mobile"),
            "reason": (self.handoff or {}).get("reason"), "signal": (self.handoff or {}).get("signal"),
            "quote": (self.handoff or {}).get("quote"), "current_step": section or phase,
            "captured": captured, "missing": self.missing(),
            "do_not_ask_again": [c["label"] for c in captured if c["confirmed"]],
            "last_turns": self.transcript[-6:], "escalation_score": self.escalation.score,
            "flags": self.flags, "consent": bool(self.consent_at),
        }

    def metrics(self) -> dict:
        end = self.ended_at or time.time()
        dur = end - self.started_at
        ai_until = self.handoff_at or end
        ai_fields = sum(1 for f in self.fields.values() if f["source"] in ("ai", "ai+llm"))
        return {"duration_s": round(dur, 1), "ai_seconds": round(ai_until - self.started_at, 1),
                "human_seconds": round(end - self.accepted_at, 1) if self.accepted_at else 0,
                "turns": sum(1 for t in self.transcript if t["speaker"] == "customer"),
                "fields_captured_by_ai": ai_fields, "reasks": self.reasks, "outcome": self.outcome,
                "handoff_signal": (self.handoff or {}).get("signal"),
                "time_to_handoff_s": (self.handoff or {}).get("at"), "llm_calls": self.llm_calls}

    def view(self) -> dict:
        return {"id": self.id, "lead": self.lead, "phase": self.phase, "outcome": self.outcome,
                "current": self.current, "fields": self.fields, "skipped": sorted(self.skipped),
                "transcript": self.transcript, "events": self.events,
                "escalation": {"score": self.escalation.score, "events": self.escalation.events[-10:]},
                "handoff": self.handoff, "card": self.card(), "human_agent": self.human_agent,
                "submission": self.submission, "metrics": self.metrics(), "script_version": self.script["version"],
                "llm": self.use_llm, "started_at": self.started_at, "simulated": self.simulated}
