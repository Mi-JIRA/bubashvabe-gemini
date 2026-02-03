import os
import re
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
import google.generativeai as genai

# Настройка логирования (чтобы видеть ошибки в Render Logs)
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "10"))

# Логика подписи Twilio
REQUIRE_TWILIO_SIGNATURE = os.getenv("REQUIRE_TWILIO_SIGNATURE", "false").lower() in (
    "1", "true", "yes", "on"
)

# Память (хранится в оперативной памяти, сбрасывается при перезапуске)
_memory: Dict[str, List[Dict[str, Any]]] = {}

# Паттерны безопасности
SENSITIVE_PATTERN = re.compile(r"\b(cvv|password|2fa|code|pin|пароль|код)\b", re.IGNORECASE)
FALLBACK_MESSAGE = "Мои мозговые жуки спят (ошибка API), попробуй позже."
SAFETY_MESSAGE = "В целях безопасности я не обрабатываю сообщения с паролями или кодами."

# Инициализация Gemini
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    logger.warning("GOOGLE_API_KEY не установлен! Бот не будет отвечать.")


def _trim_history(history: List[Dict[str, Any]], max_entries: int) -> List[Dict[str, Any]]:
    """Обрезает историю, оставляя последние N сообщений"""
    if max_entries <= 0:
        return []
    return history[-max_entries:]


def _validate_twilio_signature(request: Request, form_data: Dict[str, str]) -> bool:
    """Проверка, что запрос пришел именно от Twilio"""
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature or not TWILIO_AUTH_TOKEN:
        return False
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    # Важно: URL должен точно совпадать с тем, что настроен в Twilio (https vs http)
    # Часто Render проксирует заголовки, поэтому берем оригинальный URL если нужно,
    # но str(request.url) обычно работает, если Twilio настроен на этот же адрес.
    return validator.validate(str(request.url), form_data, signature)


def _build_twiml(message: str) -> Response:
    """Формирует XML ответ для WhatsApp"""
    response = MessagingResponse()
    response.message(message)
    return Response(content=str(response), media_type="application/xml")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    # 1. Получаем данные
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}
    
    from_number = form_data.get("From", "unknown")
    body = form_data.get("Body", "").strip()

    logger.info(f"Incoming message from {from_number}: {body}")

    # 2. Проверка безопасности (Signature)
    if REQUIRE_TWILIO_SIGNATURE:
        if not _validate_twilio_signature(request, form_data):
            logger.warning("Invalid Twilio Signature")
            return Response(content="Forbidden", status_code=403)

    # 3. Пустое сообщение?
    if not body:
        return _build_twiml("Привет! Я слушаю.")

    # 4. Проверка на секреты
    if SENSITIVE_PATTERN.search(body):
        return _build_twiml(SAFETY_MESSAGE)

    # 5. Работа с AI
    try:
        # Получаем чистую историю (БЕЗ текущего сообщения)
        current_history = list(_memory.get(from_number, []))
        
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=BUBASHVABE_SYSTEM
        )
        
        # Запускаем чат с ПРОШЛОЙ историей
        chat = model.start_chat(history=current_history)
        
        # Отправляем НОВОЕ сообщение
        result = chat.send_message(body)
        reply_text = result.text.strip()
        
        # 6. Если всё ок, обновляем память
        # Добавляем вопрос пользователя
        current_history.append({"role": "user", "parts": [body]})
        # Добавляем ответ модели
        current_history.append({"role": "model", "parts": [reply_text]})
        
        # Сохраняем обрезанную версию
        _memory[from_number] = _trim_history(current_history, MAX_HISTORY)

        return _build_twiml(reply_text)

    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        # Если ключа нет или он неверный, вернется ошибка 403/400
        if "API_KEY" in str(e) or not GOOGLE_API_KEY:
            return _build_twiml("Ошибка конфигурации: Проверьте GOOGLE_API_KEY.")
        
        return _build_twiml(FALLBACK_MESSAGE)
