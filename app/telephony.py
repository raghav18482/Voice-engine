"""Twilio phone calls via ConversationRelay: Twilio does the telephony, STT and TTS, we exchange text.

  outbound: place_call() -> Twilio dials the lead -> /twilio/voice returns relay_twiml()
  inbound:  caller dials the Twilio number -> /twilio/inbound returns relay_twiml()
  both:     Twilio opens wss://.../ws/twilio/{call_id}: 'prompt' in (caller speech), 'text' out (what to say)
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional
from urllib.parse import urlencode

from .parsers import parse_mobile

REQUIRED = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "PUBLIC_BASE_URL")


def enabled() -> bool:
    return all(os.getenv(k) for k in REQUIRED)


def public_base() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")


def phone_number() -> Optional[str]:
    return os.getenv("TWILIO_PHONE_NUMBER") or None


def to_e164(mobile: str) -> str:
    """AU mobile in any spoken/written form -> +614xxxxxxxx. Anything else is passed through untouched."""
    p = parse_mobile(mobile or "", {}, {})
    return "+61" + p.value[1:] if p.ok else (mobile or "").strip()


def url(path: str, **query) -> str:
    return f"{public_base()}{path}" + (f"?{urlencode(query)}" if query else "")


def relay_twiml(call_id: str) -> str:
    from twilio.twiml.voice_response import Connect, VoiceResponse

    ws_base = public_base().replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    relay = {"url": f"{ws_base}/ws/twilio/{call_id}", "language": os.getenv("TWILIO_LANGUAGE", "en-AU"),
             "interruptible": "speech", "dtmf_detection": True}
    if os.getenv("TWILIO_TTS_PROVIDER"):
        relay["tts_provider"] = os.getenv("TWILIO_TTS_PROVIDER")
    if os.getenv("TWILIO_TTS_VOICE"):
        relay["voice"] = os.getenv("TWILIO_TTS_VOICE")
    resp = VoiceResponse()
    connect = Connect(action=url("/twilio/relay-end", call_id=call_id))
    connect.conversation_relay(**relay)
    resp.append(connect)
    return str(resp)


def goodbye_twiml(line: Optional[str]) -> str:
    from twilio.twiml.voice_response import VoiceResponse

    resp = VoiceResponse()
    if line:
        resp.say(line, language=os.getenv("TWILIO_LANGUAGE", "en-AU"))
    resp.hangup()
    return str(resp)


def _client():
    from twilio.rest import Client

    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


async def place_call(call_id: str, to: str) -> str:
    """Dial the customer. Returns the Twilio CallSid. The SDK is sync, so it runs in a thread."""
    def _create():
        return _client().calls.create(
            to=to_e164(to), from_=os.environ["TWILIO_PHONE_NUMBER"],
            url=url("/twilio/voice", call_id=call_id),
            status_callback=url("/twilio/status", call_id=call_id),
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        ).sid
    return await asyncio.to_thread(_create)


async def hang_up(call_sid: str):
    await asyncio.to_thread(lambda: _client().calls(call_sid).update(status="completed"))


def verify(path_and_query: str, form: dict, signature: Optional[str]) -> bool:
    """Check X-Twilio-Signature against the public URL Twilio actually called (proxies rewrite the host)."""
    if os.getenv("TWILIO_SKIP_SIGNATURE", "false").lower() == "true":
        return True
    from twilio.request_validator import RequestValidator

    return RequestValidator(os.getenv("TWILIO_AUTH_TOKEN", "")).validate(
        public_base() + path_and_query, form, signature or "")
