import os
import io
import re
import time
import base64
import logging
from datetime import datetime
from threading import Thread
from typing import Dict, Any

from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
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
PORT = int(os.getenv("PORT", "8080"))
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))  # бесплатные анализы в месяц

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY")

# ---------- GEMINI ----------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------- ПАМЯТЬ ----------
LAST_ANALYSIS_AT: Dict[int, float] = {}
USAGE: Dict[int, Dict[str, Any]] = {}  # user_id -> {"count": int, "month": int, "premium": bool}

# ---------- ХЕЛПЕРЫ ----------
def check_usage(user_id: int) -> bool:
    now = datetime.utcnow()
    month = now.month
    u = USAGE.setdefault(user_id, {"count": 0, "month": month, "premium": False})

    # новый месяц → сброс
    if u["month"] != month:
        u["count"] = 0
        u["month"] = month

    if u.get("premium"):
        return True
    if u["count"] < FREE_LIMIT:
        u["count"] += 1
        return True
    return False

def get_usage_text(user_id: int) -> str:
    u = USAGE.get(user_id, {"count": 0, "month": datetime.utcnow().month, "premium": False})
    if u.get("premium"):
        return "🌟 У тебя активен Премиум (безлимит анализов)."
    left = max(0, FREE_LIMIT - u["count"])
    return f"Осталось бесплатных анализов в этом месяце: {left} из {FREE_LIMIT}."

# ---------- КЛАВЫ ----------
def action_keyboard(premium: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔄 Новый анализ", callback_data="home")],
        [InlineKeyboardButton("⚙️ Режим", callback_data="mode_menu")],
        [InlineKeyboardButton("🧑‍💼 Профиль", callback_data="profile")],
        [InlineKeyboardButton("👍 Полезно", callback_data="fb:up"),
         InlineKeyboardButton("👎 Не очень", callback_data="fb:down")],
        [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]
    ]
    if not premium:
        buttons.append([InlineKeyboardButton("🌟 Премиум", callback_data="premium")])
    else:
        buttons.append([InlineKeyboardButton("💳 Купить снова (продлить)", callback_data="renew")])
    return InlineKeyboardMarkup(buttons)

# ---------- АНАЛИЗ ----------
async def _process_image_bytes(chat, img_bytes: bytes, mode: str, user_data: dict, user_id: int):
    if not check_usage(user_id):
        return await chat.send_message(
            "🚫 Лимит бесплатных анализов исчерпан.\n\n"
            "Оформи 🌟 Премиум (безлимит):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌟 Купить Премиум", callback_data="premium")],
                [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]
            ])
        )

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
        f"Ты бьюти-ассистент. Пользователь прислал фото для режима {mode}. Дай рекомендации.",
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]

    try:
        response = model.generate_content(payload)
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            text = "Ответ пустой. Возможно, модерация или сбой."
        if len(text) > 1800:
            text = text[:1800] + "\n\n<i>Сокращено.</i>"

        u = USAGE.get(user_id, {})
        try:
            await chat.send_message(
                text, parse_mode="HTML", reply_markup=action_keyboard(u.get("premium", False))
            )
        except BadRequest:
            safe = re.sub(r"<[^>]+>", "", text)
            await chat.send_message(
                safe, reply_markup=action_keyboard(u.get("premium", False))
            )

        await chat.send_message(get_usage_text(user_id))
    except Exception as e:
        log.exception("Gemini error")
        await chat.send_message(f"Ошибка анализа: {e}")

# ---------- КОМАНДЫ ----------
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = USAGE.get(user_id, {"premium": False})
    await update.message.reply_text(
        "Привет! Я Beauty Nano Bot 💇‍♀️🤖\n"
        "Я анализирую фото лица и волос и даю советы.\n\n"
        "У тебя есть бесплатные анализы каждый месяц.\n"
        "Хочешь безлимит? Жми 🌟 Премиум.",
        reply_markup=action_keyboard(u.get("premium", False))
    )
    await update.message.reply_text(get_usage_text(user_id))

# ---------- CALLBACK ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    user_id = update.effective_user.id
    u = USAGE.setdefault(user_id, {"count": 0, "month": datetime.utcnow().month, "premium": False})

    if data == "limits":
        await q.answer()
        return await q.message.reply_text(get_usage_text(user_id))

    if data == "premium":
        await q.answer()
        return await q.message.reply_text(
            "🌟 <b>Премиум</b>\n\n"
            "• Безлимит анализов\n"
            "• Экспорт в PDF (скоро)\n"
            "• История анализов (скоро)\n\n"
            "Цена: 299 ₽ / месяц\n",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Купить", callback_data="buy")],
                [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]
            ])
        )

    if data == "buy":
        u["premium"] = True
        await q.answer()
        return await q.message.reply_text(
            "✅ Премиум активирован! Теперь у тебя безлимит.",
            reply_markup=action_keyboard(True)
        )

    if data == "renew":
        u["premium"] = True
        await q.answer("Премиум продлён")
        return await q.message.edit_text(
            "Премиум продлён ✅", reply_markup=action_keyboard(True)
        )

    if data in ("fb:up", "fb:down"):
        await q.answer("Спасибо!" if data == "fb:up" else "Принято 👍")

# ---------- ОБРАБОТКА ФОТО ----------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    now = time.time()
    last = LAST_ANALYSIS_AT.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return await update.message.reply_text("Подожди пару секунд ⏳")
    LAST_ANALYSIS_AT[user_id] = now

    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), "both", context.user_data, user_id)

# ---------- HEALTHZ ----------
def start_flask_healthz(port: int):
    app = Flask(__name__)
    @app.get("/healthz")
    def healthz(): return "ok", 200
    th = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False))
    th.daemon = True
    th.start()

# ---------- MAIN ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    start_flask_healthz(PORT)
    app.run_polling()

if __name__ == "__main__":
    main()
