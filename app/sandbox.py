"""Journey adapter + mock journey-completion sandbox.

CIMET will provide the real sandbox and payload shape on the day. Only this file changes
when it arrives: update the Pydantic models to match, and set SANDBOX_URL in .env so
submit() posts to it instead of the local mock.
"""
from __future__ import annotations

import os
import random
import string
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator


class Customer(BaseModel):
    title: Optional[str] = None
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    dob: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    email: str
    mobile: str = Field(pattern=r"^04\d{8}$")

    @field_validator("email")
    @classmethod
    def email_ok(cls, v):
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("invalid email")
        return v


class Property(BaseModel):
    property_type: Literal["residential", "business"]
    move_type: Literal["moving", "switching"]
    postcode: str = Field(pattern=r"^\d{4}$")
    state: str
    service_address: str = Field(min_length=6)
    move_in_date: Optional[str] = None
    embedded_network: bool = False


class Energy(BaseModel):
    fuel_type: Literal["electricity", "gas", "both"]
    current_retailer: Optional[str] = None
    solar: bool
    life_support: bool
    concession: bool
    last_bill_amount: Optional[int] = None


class Plan(BaseModel):
    plan_id: str
    retailer: str
    name: str


class Consent(BaseModel):
    recording_disclosed_at: str
    eic_accepted: Literal[True]
    eic_accepted_at: str
    script_version: str


class EnergyJourneyPayload(BaseModel):
    lead_id: str
    vertical: Literal["energy"] = "energy"
    channel: Literal["ai_voice", "ai_voice_with_human"] = "ai_voice"
    customer: Customer
    property: Property
    energy: Energy
    plan: Plan
    consent: Consent
    flags: list[str] = []
    call_id: str


def _ref() -> str:
    return "ECX-" + "".join(random.choices(string.digits, k=6))


def mock_sandbox(payload: dict) -> dict:
    """Behaves like a journey-completion endpoint: validates and returns a reference number."""
    try:
        p = EnergyJourneyPayload.model_validate(payload)
    except ValidationError as e:
        return {"ok": False, "status": 422,
                "errors": [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]}
    if p.property.move_type == "moving" and not p.property.move_in_date:
        return {"ok": False, "status": 422, "errors": ["property.move_in_date: required when moving"]}
    return {"ok": True, "status": 201, "reference": _ref(),
            "received_at": datetime.now(timezone.utc).isoformat()}


async def submit(payload: dict) -> dict:
    url = os.getenv("SANDBOX_URL")
    if not url:
        return mock_sandbox(payload)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload, headers={"Authorization": f"Bearer {os.getenv('SANDBOX_TOKEN', '')}"})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
        ref = body.get("reference") or body.get("reference_number") or body.get("id")
        return {"ok": r.is_success, "status": r.status_code, "reference": ref,
                "errors": [] if r.is_success else [str(body)]}
    except Exception as e:
        return {"ok": False, "status": 0, "errors": [f"Sandbox unreachable: {e}"]}
