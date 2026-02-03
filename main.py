import os
import re
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
import google.generativeai as genai

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Конфигурация
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
BUBASHVABE_SYSTEM = os.getenv(
    "BUBASHVABE_SYSTEM",
    "Ты Бубашвабе — вежливый, полезный помощник. Отвечай кратко и по делу.",
)
# Если переменная не задана, используем безопасный дефолт, который есть всегда
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "10"))

REQUIRE_TWILIO_SIGNATURE = os.getenv("REQUIRE_TWILIO_SIGNATURE", "false").lower() in (
    "1", "true", "yes", "on"
)

_memory: Dict[str, List[Dict[str, Any]]] = {}
SENSITIVE_PATTERN = re.compile(r"\b(cvv|password|2fa|code|pin|пароль|код)\b", re.IGNORECASE)
FALLBACK_MESSAGE = "Мои мозговые жуки спят (ошибка API), попробуй позже."
SAFETY_MESSAGE = "В целях безопасности я не обрабатываю сообщения с паролями или кодами."

# Инициализация
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    logger.warning("⚠️ GOOGLE_API_KEY не установлен!")

def _trim_history(history: List[Dict[str, Any]], max_entries: int) -> List[Dict[str, Any]]:
    if max_entries <= 0: return []
    return history[-max_entries:]

def _validate_twilio_signature(request: Request, form_data: Dict[str, str]) -> bool:
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature or not TWILIO_AUTH_TOKEN: return False
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(str(request.url), form_data, signature)

def _build_twiml(message: str) -> Response:
    response = MessagingResponse()
    response.message(message)
    return Response(content=str(response), media_type="application/xml")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}
    from_number = form_data.get("From", "unknown")
    body = form_data.get("Body", "").strip()

    logger.info(f"Incoming message from {from_number}: {body}")

    if REQUIRE_TWILIO_SIGNATURE:
        if not _validate_twilio_signature(request, form_data):
            logger.warning("Invalid Twilio Signature")
            return Response(content="Forbidden", status_code=403)

    if not body:
        return _build_twiml("Привет! Я слушаю.")

    if SENSITIVE_PATTERN.search(body):
        return _build_twiml(SAFETY_MESSAGE)

    try:
        current_history = list(_memory.get(from_number, []))
        
        # Явная инициализация модели внутри запроса для надежности
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=BUBASHVABE_SYSTEM
        )
        
        chat = model.start_chat(history=current_history)
        result = chat.send_message(body)
        reply_text = result.text.strip()
        
        # Обновляем память только при успехе
        current_history.append({"role": "user", "parts": [body]})
        current_history.append({"role": "model", "parts": [reply_text]})
        _memory[from_number] = _trim_history(current_history, MAX_HISTORY)

        return _build_twiml(reply_text)

    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        
        # --- DEBUG: Если ошибка 404/Not Found, выводим список доступных моделей ---
        if "404" in str(e) or "not found" in str(e).lower():
            try:
                logger.info("Listing available models for this API Key:")
                for m in genai.list_models():
                    if 'generateContent' in m.supported_generation_methods:
                        logger.info(f"- {m.name}")
            except Exception as list_err:
                logger.error(f"Could not list models: {list_err}")
        # -------------------------------------------------------------------------

        return _build_twiml(FALLBACK_MESSAGE)
