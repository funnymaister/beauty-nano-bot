import os
import io
import base64
import logging
from threading import Thread

from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

from flask import Flask  # только для healthz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, ChatMemberHandler, filters
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s"
)
log = logging.getLogger("beauty-nano-bot")

# ---------- КОНФИГ ----------
load_dotenv()
BOT_TOKEN      = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_URL    = os.getenv("WEBHOOK_URL")              # напр.: https://<app>.onrender.com/webhook
PORT           = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env/Environment")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY в .env/Environment")

# ---------- GEMINI ----------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------- РЕЖИМЫ / КЛАВЫ ----------
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

def action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Новый анализ", callback_data="home")],
            [InlineKeyboardButton("⚙️ Режим", callback_data="mode_menu")],
        ]
    )

def _hello_text() -> str:
    return (
        "Привет! Я Beauty Nano Bot (Gemini).\n"
        "Выбери режим анализа ниже и пришли фото (как Фото, не как Файл)."
    )

async def send_home(chat, user_data):
    set_mode(user_data, get_mode(user_data))
    current = get_mode(user_data)
    await chat.send_message(_hello_text(), reply_markup=mode_keyboard(current))

# ---------- ПРОМПТ (HTML) ----------
def build_prompt(mode: str) -> str:
    common = (
        "Отвечай на РУССКОМ. Ты — бережный бьюти-консультант. Дай НЕМЕДИЦИНСКИЕ советы по уходу, "
        "без диагнозов и лечения. Пиши кратко, структурно, пунктами. Используй эмодзи в заголовках. "
        "В конце добавь один общий дисклеймер одной строкой."
    )
    if mode == "face":
        specific = (
            "Анализируй ТОЛЬКО ЛИЦО. Блоки:\n"
            "⭐ <b>Что видно</b>\n"
            "🧴 <b>Тип кожи</b>\n"
            "🌞 <b>Утро</b>: 1–3 шага\n"
            "🌙 <b>Вечер</b>: 1–3 шага\n"
            "⛔ <b>Чего избегать</b>\n"
            "ℹ️ <i>Дисклеймер</i>"
        )
    elif mode == "hair":
        specific = (
            "Анализируй ТОЛЬКО ВОЛОСЫ. Блоки:\n"
            "⭐ <b>Что видно</b>\n"
            "💇 <b>Тип/состояние</b>\n"
            "🧼 <b>Мытьё и уход</b>: 1–3 шага\n"
            "💨 <b>Укладка и термозащита</b>: 1–3 шага\n"
            "⛔ <b>Чего избегать</b>\n"
            "ℹ️ <i>Дисклеймер</i>"
        )
    else:
        specific = (
            "Анализируй ЛИЦО И ВОЛОСЫ. Блоки:\n"
            "⭐ <b>Что видно</b>\n"
            "🧴 <b>Кожа</b>\n"
            "💇 <b>Волосы</b>\n"
            "🌞 <b>Утро (кожа)</b>: 1–3 шага\n"
            "🌙 <b>Вечер (кожа)</b>: 1–3 шага\n"
            "💨 <b>Волосы: уход/укладка/термозащита</b>: 1–3 шага\n"
            "⛔ <b>Чего избегать</b>\n"
            "ℹ️ <i>Дисклеймер</i>"
        )
    return f"{common}\n\nФорматируй ответ в HTML (теги <b>, <i>, переносы строк).\n\n{specific}"

# ---------- ОБРАБОТКА ФОТО ----------
async def _process_image_bytes(chat, img_bytes: bytes, mode: str, user_data: dict):
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        jpeg_bytes = buf.getvalue()
    except Exception:
        log.exception("PIL convert error")
        return await chat.send_message("Не удалось обработать фото. Попробуй другое.")

    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
    payload = [
        build_prompt(mode),
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]

    try:
        response = model.generate_content(payload)
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            text = "Ответ пустой. Возможно, сработала модерация или сбой."
        if len(text) > 1800:
            text = text[:1800] + "\n\n<i>Сокращено. Напиши /help для подсказок.</i>"
        await chat.send_message(text, parse_mode="HTML", reply_markup=action_keyboard())
    except Exception as e:
        log.exception("Gemini error")
        await chat.send_message(f"Ошибка анализа изображения: {e}")

# ---------- КОМАНДЫ / CALLBACK-и ----------
async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = get_mode(context.user_data)
    await update.message.reply_text(
        f"Текущий режим: {MODES[current]}\nВыбери другой:",
        reply_markup=mode_keyboard(current),
    )

async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Как пользоваться</b>\n"
        "1) Выбери режим: лицо/волосы/оба (/mode).\n"
        "2) Отправь фото как <i>фото</i>.\n"
        "3) Получи рекомендации.\n\n"
        "ℹ️ Это не медицинская консультация.",
        parse_mode="HTML"
    )

async def on_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Конфиденциальность</b>\n"
        "• Фото обрабатывается только в памяти.\n"
        "• Не сохраняется и не передаётся.\n"
        "• Ответ — общий уход, не замена врача.",
        parse_mode="HTML"
    )

async def on_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "home":
        return await send_home(update.effective_chat, context.user_data)
    if data == "mode_menu":
        current = get_mode(context.user_data)
        return await q.edit_message_text(
            f"Текущий режим: {MODES[current]}\nВыбери другой:",
            reply_markup=mode_keyboard(current)
        )
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        set_mode(context.user_data, mode)
        return await q.edit_message_text(
            f"Режим установлен: {MODES[mode]}\nПришли фото.",
            reply_markup=mode_keyboard(mode)
        )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data), context.user_data)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not (doc and doc.mime_type and doc.mime_type.startswith("image/")):
        return await update.message.reply_text("Пришли, пожалуйста, фото.")
    file = await doc.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data), context.user_data)

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member.new_chat_member.status == "member":
        await send_home(update.effective_chat, context.user_data)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Dispatcher error: %s", context.error)

# ---------- HEALTHZ (Flask) ----------
def start_flask_healthz(port: int):
    """Всегда поднимаем /healthz на том же порту (и в webhook, и в polling),
    чтобы Render Health Check получал 200 OK."""
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    th = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False))
    th.daemon = True
    th.start()
    log.info("Flask /healthz running on port %s", port)

# ---------- MAIN ----------
def main() -> None:
    tg_app = Application.builder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("mode", on_mode))
    tg_app.add_handler(CommandHandler("help", on_help))
    tg_app.add_handler(CommandHandler("privacy", on_privacy))
    tg_app.add_handler(CallbackQueryHandler(on_mode_callback, pattern=r"^(home|mode_menu|mode:)"))
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    tg_app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    tg_app.add_error_handler(on_error)

    # Всегда поднимаем healthz и ВСЕГДА запускаем polling
    start_flask_healthz(PORT)
    logging.warning("Force POLLING mode (WEBHOOK_URL игнорируется)")
    tg_app.run_polling()
