"""Twilio ConversationRelay wiring. No real Twilio calls: place_call / hang_up are stubbed."""
import json

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import main, telephony
from app.main import SESSIONS, app

BASE = "https://example.ngrok.app"


@pytest.fixture
def client(monkeypatch, tmp_path):
    for k, v in {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "secret", "TWILIO_PHONE_NUMBER": "+61200000000",
                 "PUBLIC_BASE_URL": BASE, "TWILIO_SKIP_SIGNATURE": "true"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(main, "ATTEMPTS_FILE", tmp_path / "dial_attempts.json")
    hung_up = []

    async def fake_place(call_id, to):
        return "CAtest" + call_id

    async def fake_hang_up(sid):
        hung_up.append(sid)

    monkeypatch.setattr(telephony, "place_call", fake_place)
    monkeypatch.setattr(telephony, "hang_up", fake_hang_up)
    c = TestClient(app)
    c.hung_up = hung_up
    return c


def test_e164():
    assert telephony.to_e164("0491 570 157") == "+61491570157"
    assert telephony.to_e164("+61491570157") == "+61491570157"


def test_health_reports_telephony(client):
    h = client.get("/api/health").json()
    assert h["telephony"] and h["phone_number"] == "+61200000000"


def test_outbound_dial_answer_and_no_answer(client):
    r = client.post("/api/calls", json={"lead_id": "1002", "mode": "phone"}).json()
    cid = r["call_id"]
    assert r["mode"] == "phone" and r["call_sid"] == "CAtest" + cid
    assert SESSIONS[cid].channel == "phone" and SESSIONS[cid].direction == "outbound"
    assert json.loads(main.ATTEMPTS_FILE.read_text()) == {"1002": 1}

    xml = client.post(f"/twilio/voice?call_id={cid}", data={"CallSid": r["call_sid"]}).text
    assert "<ConversationRelay" in xml and f'url="wss://example.ngrok.app/ws/twilio/{cid}"' in xml
    assert f"{BASE}/twilio/relay-end?call_id={cid}" in xml

    client.post(f"/twilio/status?call_id={cid}", data={"CallStatus": "no-answer"})
    assert SESSIONS[cid].outcome == "no_answer"


def test_phone_mode_needs_config(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL")
    assert client.post("/api/calls", json={"lead_id": "1002", "mode": "phone"}).status_code == 400


def test_inbound_known_lead_conversation_and_handoff(client):
    xml = client.post("/twilio/inbound", data={"From": "+61491570157", "CallSid": "CAin1"}).text
    assert "<ConversationRelay" in xml
    s = next(s for s in SESSIONS.values() if s.call_sid == "CAin1")
    assert s.direction == "inbound" and s.lead["id"] == "1002"

    with client.websocket_connect("/ws/console") as console:
        console.receive_json()  # snapshot
        with client.websocket_connect(f"/ws/twilio/{s.id}") as ws:
            ws.send_json({"type": "setup", "callSid": "CAin1", "from": "+61491570157"})
            first = ws.receive_json()
            assert first["type"] == "text" and first["last"] is True
            assert "thanks for calling" in first["token"].lower() and "recorded" in first["token"]

            ws.send_json({"type": "prompt", "voicePrompt": "yes that's fine", "lang": "en-AU", "last": True})
            assert "five minutes" in ws.receive_json()["token"]

            ws.send_json({"type": "dtmf", "digit": "0"})
            ws.receive_json()  # hold line
            assert s.phase == "handoff_pending" and "pressed 0" in s.handoff["reason"]

            # Human accepts in the console; their typed words go to the caller as speech.
            console.send_json({"type": "accept", "call_id": s.id, "agent_name": "Aarav"})
            assert "Aarav" in ws.receive_json()["token"]
            console.send_json({"type": "human_say", "call_id": s.id, "text": "Let me sort that out for you."})
            assert ws.receive_json()["token"] == "Let me sort that out for you."
    assert s.phase == "ended" and s.outcome == "customer_hung_up"
    assert client.hung_up == ["CAin1"]


def test_inbound_walk_in_and_goodbye(client):
    client.post("/twilio/inbound", data={"From": "+14155550100", "CallSid": "CAwalk"})
    s = next(s for s in SESSIONS.values() if s.call_sid == "CAwalk")
    assert s.lead["id"] == "in-0100" and s.lead["walk_in"]
    with client.websocket_connect(f"/ws/twilio/{s.id}") as ws:
        ws.send_json({"type": "setup", "callSid": "CAwalk"})
        assert "Hi there" in ws.receive_json()["token"]
        ws.send_json({"type": "prompt", "voicePrompt": "no I don't want it recorded", "last": True})
        end = ws.receive_json()
        assert end["type"] == "end" and json.loads(end["handoffData"])["outcome"] == "no_consent"
        xml = client.post(f"/twilio/relay-end?call_id={s.id}", data={"SessionStatus": "ended"}).text
        assert "<Say" in xml and "recording" in xml and "<Hangup" in xml


def test_webhook_signature(client, monkeypatch):
    monkeypatch.setenv("TWILIO_SKIP_SIGNATURE", "false")
    form = {"From": "+61491570157", "CallSid": "CAsig"}
    assert client.post("/twilio/inbound", data=form, headers={"X-Twilio-Signature": "nope"}).status_code == 403
    sig = RequestValidator("secret").compute_signature(f"{BASE}/twilio/inbound", form)
    assert client.post("/twilio/inbound", data=form, headers={"X-Twilio-Signature": sig}).status_code == 200
