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
BOT_TOKEN   = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")          # для Render: https://<app>.onrender.com/webhook
PORT        = int(os.getenv("PORT", "8080"))    # Render подставит свой, но запасной дефолт есть

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env/Environment")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY в .env/Environment")

# ---------- GEMINI ----------
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

# ---------- ПРОМПТ (красивый HTML-ответ) ----------
def build_prompt(mode: str) -> str:
    common = (
        "Отвечай на РУССКОМ. Ты — бережный бьюти-консультант. Дай НЕМЕДИЦИНСКИЕ советы по уходу, "
        "без диагнозов и лечения. Пиши кратко, структурно, пунктами. Обязательно используй эмодзи в заголовках. "
        "В конце всегда добавляй один общий дисклеймер одной строкой."
    )
    if mode == "face":
        specific = (
            "Анализируй ТОЛЬКО ЛИЦО. Верни ответ строго по блокам:\n"
            "⭐ <b>Что видно</b> (и уверенность: низкая/средняя/высокая)\n"
            "🧴 <b>Тип кожи</b> (если различимо)\n"
            "🌞 <b>Утро</b>: 1–3 шага (очищение → увлажнение → SPF)\n"
            "🌙 <b>Вечер</b>: 1–3 шага (очищение → сыворотка/увлажнение)\n"
            "⛔ <b>Чего избегать</b> (кратко)\n"
            "ℹ️ <i>Дисклеймер</i>: коротко, 1 строка."
        )
    elif mode == "hair":
        specific = (
            "Анализируй ТОЛЬКО ВОЛОСЫ. Верни ответ строго по блокам:\n"
            "⭐ <b>Что видно</b> (уверенность)\n"
            "💇 <b>Тип/состояние</b> (если различимо)\n"
            "🧼 <b>Мытьё и уход</b>: 1–3 шага\n"
            "💨 <b>Укладка и термозащита</b>: 1–3 шага\n"
            "⛔ <b>Чего избегать</b>\n"
            "ℹ️ <i>Дисклеймер</i>: коротко, 1 строка."
        )
    else:
        specific = (
            "Анализируй ЛИЦО И ВОЛОСЫ. Верни ответ строго по блокам:\n"
            "⭐ <b>Что видно</b> (уверенность)\n"
            "🧴 <b>Кожа</b>: тип/заметки (если различимо)\n"
            "💇 <b>Волосы</b>: тип/заметки (если различимо)\n"
            "🌞 <b>Утро (кожа)</b>: 1–3 шага\n"
            "🌙 <b>Вечер (кожа)</b>: 1–3 шага\n"
            "💨 <b>Волосы: уход/укладка/термозащита</b>: 1–3 шага\n"
            "⛔ <b>Чего избегать</b>\n"
            "ℹ️ <i>Дисклеймер</i>: коротко, 1 строка."
        )
    # Просим формат HTML
    return f"{common}\n\nФорматируй ответ в HTML (теги <b>, <i>, переносы строк). Не используй списки <ul>/<ol>.\n\n{specific}"

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
async def _process_image_bytes(chat, img_bytes: bytes, mode: str):
    """JPEG + base64 inline_data + вызов Gemini + HTML-ответ."""
    # Конвертация в JPEG
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        jpeg_bytes = buf.getvalue()
    except Exception:
        log.exception("PIL convert error")
        return await chat.send_message(
            "Не удалось обработать фото (конвертация). Попробуй другое изображение."
        )

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

        # Обрезка на всякий случай (лимит Телеграма ~4096)
        max_len = 1800
        if len(text) > max_len:
            text = text[:max_len] + "\n\n<i>Сокращено. Напиши /help для подсказок.</i>"

        await chat.send_message(text, parse_mode="HTML")
        log.info("Gemini OK")
    except Exception as e:
        log.exception("Gemini generate_content error")
        await chat.send_message(f"Ошибка при анализе изображения: {e}")

def _hello_text() -> str:
    return (
        "Привет! Я Beauty Nano Bot (Gemini).\n"
        "Выбери режим анализа ниже и пришли фото (как Фото, не как Файл)."
    )

# ---------- ХЭНДЛЕРЫ ----------
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context.user_data, get_mode(context.user_data))  # ensure default
    current = get_mode(context.user_data)
    await update.message.reply_text(_hello_text(), reply_markup=mode_keyboard(current))

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

async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "<b>Как пользоваться</b>\n"
        "1) Выбери режим: лицо/волосы/оба (/mode).\n"
        "2) Отправь фото как <i>фото</i> (не как файл).\n"
        "3) Получишь короткие рекомендации по уходу.\n\n"
        "<b>Подсказки</b>\n"
        "• Лучшее освещение — дневной свет спереди.\n"
        "• Фото без сильных фильтров улучшает точность.\n"
        "• Это не медицинская консультация."
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def on_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "<b>Конфиденциальность</b>\n"
        "• Фото обрабатывается в оперативной памяти и не сохраняется ботом.\n"
        "• Мы не делимся данными с третьими лицами.\n"
        "• Ответ — общий уход, не замена врача."
    )
    await update.message.reply_text(msg, parse_mode="HTML")

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
        return await update.message.reply_text("Пришли, пожалуйста, изображение (фото).")
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

# ---------- ЗАПУСК ----------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("mode", on_mode_command))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("privacy", on_privacy))
    app.add_handler(CallbackQueryHandler(on_mode_callback, pattern=r"^mode:"))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)

    if WEBHOOK_URL:
        log.info("Starting webhook: %s on port %s", WEBHOOK_URL, PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL
        )
    else:
        log.info("Starting long-polling (no WEBHOOK_URL set)")
        app.run_polling()

if __name__ == "__main__":
    main()