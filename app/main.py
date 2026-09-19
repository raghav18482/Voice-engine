"""FastAPI app: REST for leads/metrics, WebSockets for the live call and the agent console.

  customer browser  <--ws /ws/call/{id}-->  CallSession (engine)  --ws /ws/console-->  agent console
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import llm, sandbox, simulate, transcript_analysis  # noqa: E402
from .engine import DATA, CallSession, load_script  # noqa: E402
from .guardrails import gate  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
HANDOFF_TIMEOUT = int(os.getenv("HANDOFF_TIMEOUT_SECONDS", "90"))
CALLS_DIR = DATA / "calls"
CALLS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="CIMET Energy Recovery Voice Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SESSIONS: dict[str, CallSession] = {}
CUSTOMER_WS: dict[str, WebSocket] = {}
CONSOLES: set[WebSocket] = set()


def leads() -> list[dict]:
    return json.loads((DATA / "leads.json").read_text())


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


def persist(session: CallSession):
    (CALLS_DIR / f"{session.id}.json").write_text(json.dumps(session.view(), default=str, indent=1))


# ------------------------------------------------------------------ REST
@app.get("/api/health")
def health():
    return {"ok": True, "llm": llm.provider() or "rules-only", "sandbox": os.getenv("SANDBOX_URL") or "mock"}


@app.get("/api/leads")
def list_leads():
    return [{**l, "gate": gate(l)} for l in leads()]


@app.get("/api/script")
def get_script():
    return load_script()


class StartCall(BaseModel):
    lead_id: str


@app.post("/api/calls")
def start_call(body: StartCall):
    lead = lead_by_id(body.lead_id)
    g = gate(lead)
    if not g["allowed"]:
        raise HTTPException(403, detail={"message": "Blocked by pre-dial compliance gate", "gate": g})
    s = CallSession(lead)
    SESSIONS[s.id] = s
    return {"call_id": s.id, "gate": g, "llm": s.use_llm}


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
        "recent": [{"id": c["id"], "lead": c["lead"]["id"], "name": c["lead"]["first_name"],
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
