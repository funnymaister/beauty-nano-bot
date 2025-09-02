import os
import io
import base64
import logging
from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s"
)
log = logging.getLogger("beauty-nano-bot")

# ---------- КОНФИГ ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # если есть -> режим webhook
PORT = int(os.getenv("PORT", "8080"))
LISTEN_ADDR = "0.0.0.0"
WEBHOOK_PATH = "/webhook"

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY в .env")

# Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------- РЕЖИМЫ ----------
MODES = {"face": "Лицо", "hair": "Волосы", "both": "Лицо+Волосы"}

def get_mode(user_data: dict) -> str:
    return user_data.get("mode", "both")

def set_mode(user_data: dict, mode: str) -> None:
    if mode in MODES:
        user_data["mode"] = mode

def mode_keyboard(active: str) -> InlineKeyboardMarkup:
    def label(key):
        title = MODES[key]
        return f"✅ {title}" if key == active else title
    kb = [
        [InlineKeyboardButton(label("face"), callback_data="mode:face"),
         InlineKeyboardButton(label("hair"), callback_data="mode:hair")],
        [InlineKeyboardButton(label("both"), callback_data="mode:both")],
    ]
    return InlineKeyboardMarkup(kb)

def build_prompt(mode: str) -> str:
    common = (
        "Ты — бережный бьюти-консультант. Дай немедицинские рекомендации по уходу, без диагнозов. "
        "Пиши кратко, структурно, пунктами. В конце добавь дисклеймер."
    )
    if mode == "face":
        specific = (
            "Анализируй ТОЛЬКО ЛИЦО. Шаблон:\n"
            "1) Что видно (уверенность: низкая/средняя/высокая)\n"
            "2) Тип кожи (если различимо)\n"
            "3) Утро: 1–3 шага\n"
            "4) Вечер: 1–3 шага\n"
            "5) Чего избегать\n"
            "6) Дисклеймер"
        )
    elif mode == "hair":
        specific = (
            "Анализируй ТОЛЬКО ВОЛОСЫ. Шаблон:\n"
            "1) Что видно (уверенность)\n"
            "2) Тип/состояние волос (пористость/пушистость и т.п., если различимо)\n"
            "3) Мытьё и уход: 1–3 шага\n"
            "4) Укладка и термозащита: 1–3 шага\n"
            "5) Чего избегать\n"
            "6) Дисклеймер"
        )
    else:
        specific = (
            "Анализируй ЛИЦО И ВОЛОСЫ. Шаблон:\n"
            "1) Что видно (уверенность)\n"
            "2) Кожа: тип/заметки\n"
            "3) Волосы: тип/заметки\n"
            "4) Утро (кожа): 1–3 шага\n"
            "5) Вечер (кожа): 1–3 шага\n"
            "6) Волосы/укладка/термозащита: 1–3 шага\n"
            "7) Чего избегать\n"
            "8) Дисклеймер"
        )
    return f"{common}\n\n{specific}"

async def _process_image_bytes(chat, img_bytes: bytes, mode: str):
    """JPEG + base64 inline_data + вызов Gemini."""
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        jpeg_bytes = buf.getvalue()
    except Exception:
        log.exception("PIL convert error")
        await chat.send_message("Не удалось обработать фото (конвертация). Попробуй другое изображение.")
        return

    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
    payload = [
        build_prompt(mode),
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]

    try:
        response = model.generate_content(payload)
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            text = "Ответ пустой. Возможно, сработали фильтры модерации или произошёл внутренний сбой."
        await chat.send_message(text)
        log.info("Gemini OK")
    except Exception as e:
        log.exception("Gemini generate_content error")
        await chat.send_message(f"Ошибка при анализе изображения: {e}")

# ---------- ХЭНДЛЕРЫ ----------
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context.user_data, get_mode(context.user_data))  # ensure default
    current = get_mode(context.user_data)
    await update.message.reply_text(
        "Привет! Я Beauty Nano Bot (Gemini). Выбери режим анализа и пришли фото.",
        reply_markup=mode_keyboard(current),
    )

async def on_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = get_mode(context.user_data)
    await update.message.reply_text(
        f"Текущий режим: {MODES[current]}\nВыбери другой:",
        reply_markup=mode_keyboard(current),
    )

async def on_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        set_mode(context.user_data, mode)
        await q.edit_message_text(
            f"Режим установлен: {MODES[mode]}\nПришли фото.",
            reply_markup=mode_keyboard(mode)
        )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Я жду фото. Если нужно — поменяй режим анализа: /mode")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        buf = io.BytesIO()
        await file.download_to_memory(out=buf)
        await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data))
    except Exception:
        log.exception("on_photo error")
        await update.message.reply_text("Ошибка при анализе изображения. Попробуй ещё раз.")

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    mime = (doc.mime_type or "")
    if not mime.startswith("image/"):
        await update.message.reply_text("Пришли, пожалуйста, изображение (фото).")
        return
    try:
        file = await doc.get_file()
        buf = io.BytesIO()
        await file.download_to_memory(out=buf)
        await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data))
    except Exception:
        log.exception("on_document error")
        await update.message.reply_text("Ошибка при анализе изображения. Попробуй ещё раз.")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Dispatcher error: %s", context.error)

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("mode", on_mode_command))
    app.add_handler(CallbackQueryHandler(on_mode_callback, pattern=r"^mode:"))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)

    if WEBHOOK_URL:
        # режим WEBHOOK: собственный HTTP-сервер от PTB (aiohttp)
        # WEBHOOK_URL должен указывать на https://.../webhook
        log.info("Starting webhook: %s on port %s", WEBHOOK_URL, PORT)
        app.run_webhook(
            listen=LISTEN_ADDR,
            port=PORT,
            webhook_url=WEBHOOK_URL,
            secret_token=None,          # можно задать доп. секрет
            url_path=WEBHOOK_PATH       # должен совпадать с концом WEBHOOK_URL
        )
    else:
        log.info("Starting long-polling (no WEBHOOK_URL set)")
        app.run_polling()

if __name__ == "__main__":
    main()