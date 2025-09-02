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
LAST_ANALYSIS_AT: Dict[int, float] = {}  # анти-спам
USAGE: Dict[int, Dict[str, Any]] = {}    # {user_id: {"count": int, "month": int, "premium": bool}}

# ---------- МОДЫ И ПРОФИЛЬ ----------
MODES = {"face": "Лицо", "hair": "Волосы", "both": "Лицо+Волосы"}

def get_mode(user_data: dict) -> str:
    return user_data.get("mode", "both")

def set_mode(user_data: dict, mode: str) -> None:
    if mode in MODES:
        user_data["mode"] = mode

def get_profile(user_data: dict) -> Dict[str, Any]:
    return user_data.setdefault("profile", {})  # {"age","skin","hair","goals"}

def profile_to_text(pr: Dict[str, Any]) -> str:
    if not pr:
        return "Профиль пуст. Нажми «🧑‍💼 Профиль», чтобы заполнить."
    parts = []
    if pr.get("age"): parts.append(f"Возраст: {pr['age']}")
    if pr.get("skin"): parts.append(f"Кожа: {pr['skin']}")
    if pr.get("hair"): parts.append(f"Волосы: {pr['hair']}")
    if pr.get("goals"): parts.append(f"Цели: {pr['goals']}")
    return ";\n".join(parts)

def mode_keyboard(active: str) -> InlineKeyboardMarkup:
    def label(key):
        title = MODES[key]
        return f"✅ {title}" if key == active else title
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("face"), callback_data="mode:face"),
         InlineKeyboardButton(label("hair"), callback_data="mode:hair")],
        [InlineKeyboardButton(label("both"), callback_data="mode:both")],
    ])

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

def hello_text() -> str:
    return (
        "Привет! Я Beauty Nano Bot (Gemini).\n"
        "Выбери режим анализа ниже и пришли фото (как Фото, не как Файл)."
    )

# ---------- ЛИМИТЫ ----------
def check_usage(user_id: int) -> bool:
    now = datetime.utcnow()
    month = now.month
    u = USAGE.setdefault(user_id, {"count": 0, "month": month, "premium": False})
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

# ---------- ПРОМПТ ----------
def build_prompt(mode: str, profile: Dict[str, Any]) -> str:
    prof_lines = []
    if profile.get("age"): prof_lines.append(f"возраст: {profile['age']}")
    if profile.get("skin"): prof_lines.append(f"кожа: {profile['skin']}")
    if profile.get("hair"): prof_lines.append(f"волосы: {profile['hair']}")
    if profile.get("goals"): prof_lines.append(f"цели: {profile['goals']}")
    prof_text = "; ".join(prof_lines)

    profile_hint = (
        f"Учитывай профиль пользователя ({prof_text}). "
        "Если визуально есть расхождения с профилем — отметь деликатно."
        if prof_text else
        "Если сможешь — уточняй при необходимости данные профиля."
    )

    common = (
        "Отвечай на РУССКОМ. Ты — бережный бьюти-консультант. Дай НЕМЕДИЦИНСКИЕ советы по уходу, "
        "без диагнозов и лечения. Пиши кратко, структурно, пунктами с эмодзи в заголовках. "
        "В конце — один общий дисклеймер одной строкой."
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
    return f"{common}\n{profile_hint}\n\nФорматируй ответ в HTML.\n\n{specific}"

# ---------- HEALTHZ ----------
def start_flask_healthz(port: int):
    app = Flask(__name__)
    @app.get("/healthz")
    def healthz(): return "ok", 200
    th = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False))
    th.daemon = True
    th.start()
    log.info("Flask /healthz running on port %s", port)

# ---------- UI/НАВИГАЦИЯ ----------
async def send_home(chat, user_data):
    current = get_mode(user_data)
    await chat.send_message(hello_text(), reply_markup=mode_keyboard(current))

# ---------- ОБРАБОТКА ФОТО ----------
async def _process_image_bytes(chat, img_bytes: bytes, mode: str, user_data: dict, user_id: int):
    # лимиты
    if not check_usage(user_id):
        return await chat.send_message(
            "🚫 Лимит бесплатных анализов исчерпан.\n\nОформи 🌟 Премиум (безлимит):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌟 Купить Премиум", callback_data="premium")],
                [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]
            ])
        )

    # подготовка изображения
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
        build_prompt(mode, get_profile(user_data)),
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]

    try:
        response = model.generate_content(payload)
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            text = "Ответ пустой. Возможно, модерация или сбой."
        if len(text) > 1800:
            text = text[:1800] + "\n\n<i>Сокращено.</i>"

        premium = USAGE.get(user_id, {}).get("premium", False)
        try:
            await chat.send_message(
                text, parse_mode="HTML", reply_markup=action_keyboard(premium), disable_web_page_preview=True
            )
        except BadRequest:
            safe = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
            safe = re.sub(r"(?i)</?(p|div|ul|ol|li|h[1-6])[^>]*>", "\n", safe)
            safe = re.sub(r"<[^>]+>", "", safe)
            safe = re.sub(r"\n{3,}", "\n\n", safe).strip()
            await chat.send_message(safe, reply_markup=action_keyboard(premium), disable_web_page_preview=True)

        await chat.send_message(get_usage_text(user_id))
    except Exception as e:
        log.exception("Gemini error")
        await chat.send_message(f"Ошибка анализа: {e}")

# ---------- ХЭНДЛЕРЫ КОМАНД И ТЕКСТА ----------
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["welcomed"] = True
    await send_home(update.effective_chat, context.user_data)
    await update.message.reply_text(get_usage_text(update.effective_user.id))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # если идёт мастер профиля
    stage = context.user_data.get("ps")  # "age"|"skin"|"hair"|"goals"
    if stage:
        txt = (update.message.text or "").strip()
        prof = get_profile(context.user_data)
        if stage == "age":
            if not txt.isdigit() or not (5 <= int(txt) <= 100):
                return await update.message.reply_text("Введите возраст числом от 5 до 100.")
            prof["age"] = int(txt)
            context.user_data["ps"] = "skin"
            return await update.message.reply_text("Опиши тип кожи (напр.: нормальная/жирная/сухая/комбинированная, чувствительная/нет):")
        if stage == "skin":
            prof["skin"] = txt[:100]
            context.user_data["ps"] = "hair"
            return await update.message.reply_text("Какой тип/состояние волос? (напр.: тонкие, окрашенные, склонны к жирности...)")
        if stage == "hair":
            prof["hair"] = txt[:120]
            context.user_data["ps"] = "goals"
            return await update.message.reply_text("Какие цели/предпочтения? (напр.: меньше блеска, объём, без сульфатов)")
        if stage == "goals":
            prof["goals"] = txt[:160]
            context.user_data["ps"] = None
            return await update.message.reply_text("Готово! Твой профиль:\n\n" + profile_to_text(prof))

    # обычный текст: подсказываем
    current = get_mode(context.user_data)
    await update.message.reply_text(
        "Я жду фото 🙂\nМожно сменить режим: /start → выбери в меню «⚙️ Режим»",
        reply_markup=mode_keyboard(current)
    )

# ---------- CALLBACK-КНОПКИ ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = (q.data or "").strip()
    user_id = update.effective_user.id
    u = USAGE.setdefault(user_id, {"count": 0, "month": datetime.utcnow().month, "premium": False})

    # Навигация
    if data == "home":
        await q.answer()
        return await send_home(update.effective_chat, context.user_data)

    if data == "mode_menu":
        await q.answer()
        current = get_mode(context.user_data)
        return await q.message.edit_text(
            f"Текущий режим: {MODES[current]}\nВыбери другой:",
            reply_markup=mode_keyboard(current)
        )

    if data.startswith("mode:"):
        await q.answer("Режим обновлён")
        mode = data.split(":", 1)[1]
        set_mode(context.user_data, mode)
        return await q.message.edit_text(
            f"Режим установлен: {MODES[mode]}\nПришли фото.",
            reply_markup=mode_keyboard(mode)
        )

    if data == "profile":
        await q.answer()
        context.user_data["ps"] = "age"
        return await q.message.reply_text(
            "Давай настроим профиль ✨\nСколько тебе лет? (число, например 25)\n\nНапиши /cancel чтобы отменить."
        )

    # Монетизация
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
        return await q.message.edit_text("Премиум продлён ✅", reply_markup=action_keyboard(True))

    # Фидбек
    if data in ("fb:up", "fb:down"):
        await q.answer("Спасибо!" if data == "fb:up" else "Принято 👍")
        log.info("Feedback %s: %s", user_id, data)
        return

# ---------- ИЗОБРАЖЕНИЯ ----------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    now = time.time()
    last = LAST_ANALYSIS_AT.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return await update.message.reply_text("Подожди пару секунд ⏳")
    LAST_ANALYSIS_AT[user_id] = now

    # скачиваем фото
    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    await _process_image_bytes(
        update.effective_chat, buf.getvalue(), get_mode(context.user_data), context.user_data, user_id
    )

# ---------- АВТОПРИВЕТ ----------
async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member.new_chat_member.status == "member":
        context.user_data["welcomed"] = True
        await send_home(update.effective_chat, context.user_data)

# ---------- ОШИБКИ ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Dispatcher error: %s", context.error)

# ---------- MAIN ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)

    start_flask_healthz(PORT)
    logging.warning("Force POLLING mode (/healthz активен)")
    app.run_polling()

if __name__ == "__main__":
    main()
