import os
import re
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

import google.generativeai as genai

app = FastAPI()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
BUBASHVABE_SYSTEM = os.getenv(
    "BUBASHVABE_SYSTEM",
    "You are Bubashvabe, a helpful assistant.",
)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "10"))

REQUIRE_TWILIO_SIGNATURE = os.getenv("REQUIRE_TWILIO_SIGNATURE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_memory: Dict[str, List[Dict[str, Any]]] = {}

SENSITIVE_PATTERN = re.compile(r"\b(cvv|password|2fa|code)\b", re.IGNORECASE)
FALLBACK_MESSAGE = "My brain bugs are sleeping, try again"
SAFETY_MESSAGE = (
    "For your safety, I can't help with sensitive data like passwords or codes."
)


def _trim_history(history: List[Dict[str, Any]], max_entries: int) -> List[Dict[str, Any]]:
    if max_entries <= 0:
        return []
    if len(history) <= max_entries:
        return history
    return history[-max_entries:]


def _validate_twilio_signature(request: Request, form_data: Dict[str, str]) -> bool:
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature or not TWILIO_AUTH_TOKEN:
        return False
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(str(request.url), form_data, signature)


def _build_twiml(message: str) -> Response:
    response = MessagingResponse()
    response.message(message)
    return Response(content=str(response), media_type="application/xml")


def _get_model() -> genai.GenerativeModel:
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set.")
    genai.configure(api_key=GOOGLE_API_KEY)
    return genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=BUBASHVABE_SYSTEM,
    )


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}

    if REQUIRE_TWILIO_SIGNATURE and not _validate_twilio_signature(request, form_data):
        return Response(content="Forbidden", status_code=403)

    from_number = form_data.get("From", "unknown")
    body = form_data.get("Body", "").strip()

    if not body:
        return _build_twiml("Send a message so I can help.")

    if SENSITIVE_PATTERN.search(body):
        return _build_twiml(SAFETY_MESSAGE)

    history = list(_memory.get(from_number, []))
    history.append({"role": "user", "parts": [body]})
    history = _trim_history(history, MAX_HISTORY)

    try:
        model = _get_model()
        chat = model.start_chat(history=history)
        result = chat.send_message(body)
        reply_text = result.text.strip() if result and result.text else FALLBACK_MESSAGE
    except Exception:
        reply_text = FALLBACK_MESSAGE

    history.append({"role": "model", "parts": [reply_text]})
    history = _trim_history(history, MAX_HISTORY)
    _memory[from_number] = history

    return _build_twiml(reply_text)
