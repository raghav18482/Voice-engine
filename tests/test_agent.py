import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import simulate
from app.guardrails import redact_cards
from app.main import app
from app.parsers import parse


@pytest.mark.parametrize("ftype,text,expected", [
    ("mobile", "zero four nine one five seven zero one five six", "0491570156"),
    ("postcode", "two zero zero zero", "2000"),
    ("email", "j dot smith at gmial dot com", "j.smith@gmail.com"),
    ("dob", "twelfth of march nineteen eighty five", "1985-03-12"),
    ("money", "about two hundred and fifty dollars", 250),
])
def test_parsers(ftype, text, expected):
    p = parse(ftype, text, {}, {"today": date(2026, 9, 19)})
    assert p.ok and p.value == expected


def test_dob_under_18_rejected():
    assert not parse("dob", "1 January 2015", {}, {"today": date(2026, 9, 19)}).ok


def test_card_redaction():
    red, found = redact_cards("my card is 4111 1111 1111 1111 expiry 12 28")
    assert found and "4111" not in red


def test_all_personas():
    out = asyncio.run(simulate.run_all())
    failed = [r["persona"] for r in out["results"] if not r["pass"]]
    assert not failed, failed
    assert all(not g["allowed"] for g in out["gate"])


def test_api_call_over_websocket():
    c = TestClient(app)
    assert c.post("/api/calls", json={"lead_id": "1004"}).status_code == 403  # DNC stub blocks
    r = c.post("/api/calls", json={"lead_id": "1002"})
    call_id = r.json()["call_id"]
    with c.websocket_connect("/ws/console") as console:
        assert console.receive_json()["type"] == "snapshot"
        with c.websocket_connect(f"/ws/call/{call_id}") as ws:
            first = ws.receive_json()
            assert first["type"] == "say" and "recorded" in first["text"]
            ws.send_json({"type": "utterance", "text": "yes", "confidence": 0.9})
            assert "five minutes" in ws.receive_json()["text"]
            ws.send_json({"type": "utterance", "text": "can I talk to a real person", "confidence": 0.9})
            msgs = [ws.receive_json(), ws.receive_json()]
            assert any(m["type"] == "handoff" for m in msgs)
            # console accepts the warm transfer
            while True:
                m = console.receive_json()
                if m["type"] == "call_update" and m["call"]["phase"] == "handoff_pending":
                    assert m["call"]["card"]["reason"]
                    break
            console.send_json({"type": "accept", "call_id": call_id, "agent_name": "Aarav"})
            greet = ws.receive_json()
            assert greet["speaker"] == "human" and "no need to repeat" in greet["text"]
