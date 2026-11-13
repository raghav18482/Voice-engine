"""FastAPI app: REST for leads/metrics, WebSockets for the live call and the agent console.

  customer browser  <--ws /ws/call/{id}-->  CallSession (engine)  --ws /ws/console-->  agent console
  customer phone  <--Twilio ConversationRelay ws /ws/twilio/{id}-->  CallSession  (see telephony.py)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import llm, sandbox, simulate, telephony, transcript_analysis  # noqa: E402
from .engine import DATA, CallSession, load_script  # noqa: E402
from .guardrails import gate, opt_outs  # noqa: E402
from .parsers import parse_mobile  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
HANDOFF_TIMEOUT = int(os.getenv("HANDOFF_TIMEOUT_SECONDS", "90"))
SILENCE_SECONDS = float(os.getenv("TWILIO_SILENCE_SECONDS", "12"))
CALLS_DIR = DATA / "calls"
CALLS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="CIMET Energy Recovery Voice Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SESSIONS: dict[str, CallSession] = {}
CUSTOMER_WS: dict[str, WebSocket] = {}
CONSOLES: set[WebSocket] = set()
PHONE_WS: dict[str, WebSocket] = {}  # Twilio ConversationRelay socket per phone call
GOODBYE: dict[str, str] = {}  # last line of a phone call, spoken by /twilio/relay-end before hanging up
CALL_STATUS: dict[str, str] = {}  # latest Twilio CallStatus per phone call
SILENCE: dict[str, asyncio.Task] = {}
ATTEMPTS_FILE = DATA / "dial_attempts.json"


def dial_attempts() -> dict[str, int]:
    return json.loads(ATTEMPTS_FILE.read_text()) if ATTEMPTS_FILE.exists() else {}


def bump_attempts(lead_id: str):
    a = dial_attempts()
    a[lead_id] = a.get(lead_id, 0) + 1
    ATTEMPTS_FILE.write_text(json.dumps(a, indent=1))


def leads() -> list[dict]:
    """leads.json plus real phone dials made since (so the attempt cap counts them)."""
    extra = dial_attempts()
    rows = json.loads((DATA / "leads.json").read_text())
    local = DATA / "leads.local.json"  # your own test numbers, gitignored
    if local.exists():
        rows += json.loads(local.read_text())
    return [{**l, "attempts": l.get("attempts", 0) + extra.get(l["id"], 0)} for l in rows]


def lead_by_id(lid: str) -> dict:
    for l in leads():
        if l["id"] == lid:
            return l
    raise HTTPException(404, "Lead not found")


# ------------------------------------------------------------------ fan-out
async def broadcast_console(msg: dict):
    dead = []
    for ws in list(CONSOLES):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CONSOLES.discard(ws)


async def dispatch(session: CallSession, events: list[dict]):
    """Send engine output to the customer page and push the new call state to consoles."""
    ws = CUSTOMER_WS.get(session.id)
    if session.channel == "phone":
        await to_phone(session, events)
    for ev in events:
        if ws and ev["type"] in ("say", "ended", "handoff", "submitted"):
            try:
                await ws.send_json(ev)
            except Exception:
                pass
        if ev["type"] == "handoff":
            asyncio.create_task(handoff_timer(session.id))
        if ev["type"] == "ended":
            persist(session)
    await broadcast_console({"type": "call_update", "call": session.view()})


async def handoff_timer(call_id: str):
    await asyncio.sleep(HANDOFF_TIMEOUT)
    s = SESSIONS.get(call_id)
    if s and s.phase == "handoff_pending":
        await dispatch(s, s.handoff_timeout())


# ------------------------------------------------------------------ phone (Twilio)
async def to_phone(session: CallSession, events: list[dict]):
    """Engine events -> ConversationRelay messages. The goodbye is spoken by /twilio/relay-end, so it isn't cut off."""
    says = [ev["text"] for ev in events if ev["type"] == "say"]
    ended = any(ev["type"] == "ended" for ev in events)
    ws = PHONE_WS.get(session.id)
    if ended:
        cancel_silence(session.id)
        GOODBYE[session.id] = " ".join(says)
        if ws:
            try:
                await ws.send_json({"type": "end", "handoffData": json.dumps({"outcome": session.outcome})})
            except Exception:
                pass
        elif session.call_sid and CALL_STATUS.get(session.id) not in TERMINAL:
            asyncio.create_task(safe_hang_up(session.call_sid))  # ended from the console before they answered
        return
    if not ws or not says:
        return
    for text in says:
        try:
            await ws.send_json({"type": "text", "token": text, "last": True})
        except Exception:
            return
    arm_silence(session, sum(len(t.split()) for t in says))


TERMINAL = {"completed", "busy", "no-answer", "failed", "canceled"}


async def safe_hang_up(call_sid: str):
    try:
        await telephony.hang_up(call_sid)
    except Exception as e:  # already finished, or Twilio unreachable
        log.info("hang_up %s: %s", call_sid, e)


def cancel_silence(call_id: str):
    t = SILENCE.pop(call_id, None)
    if t and t is not asyncio.current_task():
        t.cancel()


def arm_silence(session: CallSession, words: int):
    """Twilio only sends 'prompt' when the caller speaks, so silence has to be timed here."""
    cancel_silence(session.id)
    if session.phase in ("handoff_pending", "human", "ended"):
        return
    wait = SILENCE_SECONDS + words / 2.5  # roughly how long the agent takes to say it
    SILENCE[session.id] = asyncio.create_task(silence_timer(session.id, wait))


async def silence_timer(call_id: str, wait: float):
    await asyncio.sleep(wait)
    s = SESSIONS.get(call_id)
    if not s or s.phase in ("handoff_pending", "human", "ended"):
        return
    SILENCE.pop(call_id, None)
    s.retries["_silence"] += 1
    if s.retries["_silence"] >= 2:
        s.log("caller_silent", "No answer after a re-prompt")
        s.end("callback", "I'll let you go for now, and we'll try you again at a better time. Bye for now.")
    else:
        s.say("Sorry, are you still there? " + s.last_prompt)
    await dispatch(s, s.drain())


def mobile_local(number: str) -> str:
    p = parse_mobile(number or "", {}, {})
    return p.value if p.ok else (number or "")


def persist(session: CallSession):
    (CALLS_DIR / f"{session.id}.json").write_text(json.dumps(session.view(), default=str, indent=1))


# ------------------------------------------------------------------ REST
@app.get("/api/health")
def health():
    return {"ok": True, "llm": llm.provider() or "rules-only", "sandbox": os.getenv("SANDBOX_URL") or "mock",
            "telephony": telephony.enabled(), "phone_number": telephony.phone_number() if telephony.enabled() else None}


@app.get("/api/leads")
def list_leads():
    return [{**l, "gate": gate(l)} for l in leads()]


@app.get("/api/script")
def get_script():
    return load_script()


class StartCall(BaseModel):
    lead_id: str
    mode: str = "browser"  # browser (you play the customer) | phone (Twilio dials the lead's mobile)


@app.post("/api/calls")
async def start_call(body: StartCall):
    lead = lead_by_id(body.lead_id)
    g = gate(lead)
    if not g["allowed"]:
        raise HTTPException(403, detail={"message": "Blocked by pre-dial compliance gate", "gate": g})
    if body.mode != "phone":
        s = CallSession(lead)
        SESSIONS[s.id] = s
        return {"call_id": s.id, "gate": g, "llm": s.use_llm, "mode": "browser"}
    if not telephony.enabled():
        raise HTTPException(400, detail={"message": "Phone calls need TWILIO_* and PUBLIC_BASE_URL in backend/.env"})
    s = CallSession(lead, channel="phone")
    SESSIONS[s.id] = s
    try:
        s.call_sid = await telephony.place_call(s.id, lead["mobile"])
    except Exception as e:
        SESSIONS.pop(s.id, None)
        raise HTTPException(502, detail={"message": f"Twilio could not place the call: {e}"})
    bump_attempts(lead["id"])
    s.log("dialing", f"{telephony.to_e164(lead['mobile'])} ({s.call_sid})")
    await dispatch(s, s.drain())
    return {"call_id": s.id, "gate": g, "llm": s.use_llm, "mode": "phone", "call_sid": s.call_sid}


@app.get("/api/calls")
def list_calls():
    live = [s.view() for s in SESSIONS.values()]
    return sorted(live, key=lambda c: -c["started_at"])


@app.get("/api/calls/{call_id}")
def get_call(call_id: str):
    if call_id in SESSIONS:
        return SESSIONS[call_id].view()
    p = CALLS_DIR / f"{call_id}.json"
    if p.exists():
        return json.loads(p.read_text())
    raise HTTPException(404)


@app.get("/api/metrics")
def metrics():
    calls = {}
    for p in CALLS_DIR.glob("*.json"):
        try:
            c = json.loads(p.read_text())
            calls[c["id"]] = c
        except Exception:
            pass
    for s in SESSIONS.values():
        calls[s.id] = s.view()
    rows = [c for c in calls.values() if not c.get("simulated")]
    done = [c for c in rows if c.get("outcome")]
    outcomes: dict[str, int] = {}
    handoffs: dict[str, int] = {}
    for c in done:
        o = c["outcome"] or "in_progress"
        o = "handoff" if o.startswith("handoff") else o
        outcomes[o] = outcomes.get(o, 0) + 1
    for c in rows:
        if c.get("handoff"):
            handoffs[c["handoff"]["signal"]] = handoffs.get(c["handoff"]["signal"], 0) + 1
    submitted = [c for c in rows if (c.get("outcome") or "").startswith("submitted")]
    ai_only = [c for c in submitted if not c.get("human_agent")]
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    baseline = transcript_analysis.baseline_minutes()
    ai_minutes = avg([c["metrics"]["duration_s"] / 60 for c in ai_only])
    return {
        "calls": len(rows), "outcomes": outcomes, "handoff_reasons": handoffs,
        "submitted": len(submitted), "submitted_without_human": len(ai_only),
        "avg_ai_call_minutes": ai_minutes, "manual_baseline_minutes": baseline,
        "human_minutes_saved": round(baseline * len(ai_only), 1),
        "avg_reasks": avg([c["metrics"]["reasks"] for c in rows]),
        "avg_fields_by_ai": avg([c["metrics"]["fields_captured_by_ai"] for c in rows]),
        "recent": [{"id": c["id"], "lead": c["lead"]["id"], "name": c["lead"].get("first_name") or "Caller",
                    "outcome": c.get("outcome") or c.get("phase"), "duration_s": c["metrics"]["duration_s"],
                    "reasks": c["metrics"]["reasks"], "handoff": (c.get("handoff") or {}).get("signal")}
                   for c in sorted(rows, key=lambda c: -c["started_at"])[:20]],
    }


@app.post("/api/simulate")
async def run_simulation():
    out = await simulate.run_all()
    for r in out["results"]:
        r["transcript"] = r["transcript"][-8:]
        r.pop("events", None)
    return out


class TranscriptIn(BaseModel):
    text: Optional[str] = None


@app.get("/api/insights")
def insights():
    return transcript_analysis.analyse()


@app.post("/api/insights")
def insights_custom(body: TranscriptIn):
    return transcript_analysis.analyse(body.text)


@app.post("/api/sandbox/journeys", status_code=201)
def mock_sandbox_endpoint(payload: dict):
    """Stand-in for CIMET's journey-completion sandbox, same contract as sandbox.mock_sandbox."""
    res = sandbox.mock_sandbox(payload)
    if not res["ok"]:
        raise HTTPException(422, res["errors"])
    return res


@app.post("/api/optouts/reset")
def reset_optouts():
    (DATA / "optouts.json").write_text("[]")
    return {"ok": True}


# ------------------------------------------------------------------ Twilio webhooks
async def twilio_form(request: Request) -> dict:
    form = dict(await request.form())
    pq = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not telephony.verify(pq, form, request.headers.get("X-Twilio-Signature")):
        raise HTTPException(403, "Bad Twilio signature")
    return form


def twiml(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


@app.post("/twilio/voice")
async def twilio_voice(request: Request, call_id: str):
    """Outbound call answered: hand the audio to ConversationRelay."""
    await twilio_form(request)
    if call_id not in SESSIONS:
        return twiml(telephony.goodbye_twiml(None))
    return twiml(telephony.relay_twiml(call_id))


@app.post("/twilio/inbound")
async def twilio_inbound(request: Request):
    """Voice webhook of the Twilio number. Known leads resume their journey; anyone else starts fresh."""
    form = await twilio_form(request)
    caller = form.get("From", "")
    local = mobile_local(caller)
    lead = next((l for l in leads() if mobile_local(l.get("mobile", "")) == local), None)
    walk_in = lead is None
    if walk_in:
        digits = "".join(ch for ch in caller if ch.isdigit())
        lead = {"id": f"in-{digits[-4:] or 'anon'}", "mobile": local, "prefilled": {}, "attempts": 0,
                "source": "inbound call", "last_completed_step": "none", "walk_in": True}
    s = CallSession(lead, direction="inbound", channel="phone")
    s.call_sid = form.get("CallSid")
    SESSIONS[s.id] = s
    s.log("inbound_call", f"From {caller}" + (" (new caller)" if walk_in else f" matched lead {lead['id']}"))
    if not walk_in and local in opt_outs():
        s.log("opted_out_caller", "Caller is on the opt-out list but rang us, so the call continues")
    await dispatch(s, s.drain())
    return twiml(telephony.relay_twiml(s.id))


@app.post("/twilio/relay-end")
async def twilio_relay_end(request: Request, call_id: str):
    """<Connect action>: runs when the relay session ends. Speak the goodbye, then hang up."""
    form = await twilio_form(request)
    s = SESSIONS.get(call_id)
    if s and form.get("SessionStatus") == "failed":
        s.log("telephony_error", f"{form.get('ErrorCode')}: {form.get('ErrorMessage')}")
        if s.phase != "ended":
            s.end("telephony_error")
            await dispatch(s, s.drain())
        return twiml(telephony.goodbye_twiml("Sorry, we're having trouble with this line. We'll call you back shortly."))
    if s and s.phase != "ended":
        s.end("customer_hung_up")
        await dispatch(s, s.drain())
    return twiml(telephony.goodbye_twiml(GOODBYE.pop(call_id, None)))


@app.post("/twilio/status")
async def twilio_status(request: Request, call_id: str):
    """Call progress for outbound dials: ringing, no answer, busy, completed."""
    form = await twilio_form(request)
    status = form.get("CallStatus", "")
    CALL_STATUS[call_id] = status
    s = SESSIONS.get(call_id)
    if not s:
        return Response(status_code=204)
    s.log("phone_status", status)
    if s.phase == "init" and status in ("busy", "no-answer", "failed", "canceled"):
        s.end({"no-answer": "no_answer", "busy": "busy"}.get(status, "telephony_error"))
    elif s.phase != "ended" and status == "completed":
        s.end("customer_hung_up")
    await dispatch(s, s.drain())
    return Response(status_code=204)


# ------------------------------------------------------------------ WebSockets
@app.websocket("/ws/call/{call_id}")
async def ws_call(ws: WebSocket, call_id: str):
    await ws.accept()
    s = SESSIONS.get(call_id)
    if not s:
        await ws.send_json({"type": "error", "message": "Unknown call"})
        await ws.close()
        return
    CUSTOMER_WS[call_id] = ws
    if s.phase == "init":
        await dispatch(s, s.start())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "utterance":
                events = await s.handle(msg.get("text", ""), msg.get("confidence"))
                await dispatch(s, events)
            elif msg.get("type") == "hangup":
                if s.phase != "ended":
                    s.end("customer_hung_up")
                    await dispatch(s, s.drain())
                break
    except WebSocketDisconnect:
        if s.phase not in ("ended",):
            s.end("customer_hung_up")
            await dispatch(s, s.drain())
    finally:
        CUSTOMER_WS.pop(call_id, None)


@app.websocket("/ws/twilio/{call_id}")
async def ws_twilio(ws: WebSocket, call_id: str):
    """ConversationRelay session: 'prompt' is what the caller said, we answer with 'text'."""
    await ws.accept()
    s = SESSIONS.get(call_id)
    if not s:
        await ws.send_json({"type": "end", "handoffData": json.dumps({"error": "unknown call"})})
        await ws.close()
        return
    PHONE_WS[call_id] = ws
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            if t == "setup":
                s.call_sid = s.call_sid or msg.get("callSid")
                if s.phase == "init":
                    await dispatch(s, s.start())
            elif t == "prompt" and msg.get("last", True):
                cancel_silence(call_id)
                s.retries["_silence"] = 0
                await dispatch(s, await s.handle(msg.get("voicePrompt", "")))
            elif t == "interrupt":
                s.log("barge_in", f"Caller spoke over: \"{msg.get('utteranceUntilInterrupt', '')}\"")
                await dispatch(s, s.drain())
            elif t == "dtmf" and msg.get("digit") == "0" and s.phase not in ("handoff_pending", "human", "ended"):
                cancel_silence(call_id)
                s.do_handoff("asks", "Caller pressed 0 for a human")
                await dispatch(s, s.drain())
            elif t == "error":
                log.warning("ConversationRelay error on %s: %s", call_id, msg.get("description"))
    except WebSocketDisconnect:
        pass
    finally:
        PHONE_WS.pop(call_id, None)
        cancel_silence(call_id)
        if s.phase != "ended":
            s.end("customer_hung_up")
            await dispatch(s, s.drain())


@app.websocket("/ws/console")
async def ws_console(ws: WebSocket):
    await ws.accept()
    CONSOLES.add(ws)
    await ws.send_json({"type": "snapshot", "calls": [s.view() for s in SESSIONS.values()]})
    try:
        while True:
            msg = await ws.receive_json()
            s = SESSIONS.get(msg.get("call_id", ""))
            if not s:
                continue
            t = msg.get("type")
            if t == "accept":
                await dispatch(s, s.accept_handoff(msg.get("agent_name") or "Aarav"))
            elif t == "force_handoff":
                s.do_handoff("asks", "Supervisor took over from the console")
                await dispatch(s, s.drain())
            elif t == "human_say":
                await dispatch(s, s.human_say(msg.get("text", "")))
            elif t == "set_field":
                await dispatch(s, s.human_set_field(msg["field"], msg.get("value", "")))
            elif t == "submit":
                s._store("eic_accepted", True, "human", confirmed=True)
                res = await s.submit(agent="human")
                await dispatch(s, s.drain())
                await ws.send_json({"type": "submit_result", "call_id": s.id, "result": res})
            elif t == "end":
                s.end(s.outcome or "ended_by_agent", msg.get("line") or "Thanks for your time, goodbye.")
                await dispatch(s, s.drain())
    except WebSocketDisconnect:
        CONSOLES.discard(ws)


# ------------------------------------------------------------------ serve the built console (optional)
_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_DIST, html=True), name="console")
