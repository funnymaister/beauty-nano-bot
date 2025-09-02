import os
import io
import re
import time
import base64
import logging
from threading import Thread
from typing import Dict, Any

from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

from flask import Flask  # для /healthz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, ChatMemberHandler, ConversationHandler,
    filters
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
PORT           = int(os.getenv("PORT", "8080"))   # Render подставит свой
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env/Environment")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY в .env/Environment")

# ---------- GEMINI ----------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------- РЕЖИМЫ / КЛАВЫ / RATE-LIMIT ----------
MODES = {"face": "Лицо", "hair": "Волосы", "both": "Лицо+Волосы"}
LAST_ANALYSIS_AT: Dict[int, float] = {}  # {user_id: timestamp}

# ---------- ПРОФИЛЬ ----------
# Состояния ConversationHandler
P_AGE, P_SKIN, P_HAIR, P_GOALS, P_DONE = range(5)

def get_profile(user_data: dict) -> Dict[str, Any]:
    return user_data.setdefault("profile", {})

def profile_to_text(pr: Dict[str, Any]) -> str:
    if not pr:
        return "Профиль пуст. Нажми «🧑‍💼 Профиль» или /profile, чтобы заполнить."
    parts = []
    if pr.get("age"): parts.append(f"Возраст: {pr['age']}")
    if pr.get("skin"): parts.append(f"Кожа: {pr['skin']}")
    if pr.get("hair"): parts.append(f"Волосы: {pr['hair']}")
    if pr.get("goals"): parts.append(f"Цели: {pr['goals']}")
    return ";\n".join(parts)

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
            [InlineKeyboardButton("🧑‍💼 Профиль", callback_data="profile")],
            [InlineKeyboardButton("👍 Полезно", callback_data="fb:up"),
             InlineKeyboardButton("👎 Не очень", callback_data="fb:down")],
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

# ---------- ПРОМПТ (HTML) с учётом ПРОФИЛЯ ----------
def build_prompt(mode: str, profile: Dict[str, Any]) -> str:
    # Встраиваем профиль пользователя
    prof_lines = []
    if profile.get("age"): prof_lines.append(f"возраст: {profile['age']}")
    if profile.get("skin"): prof_lines.append(f"кожа: {profile['skin']}")
    if profile.get("hair"): prof_lines.append(f"волосы: {profile['hair']}")
    if profile.get("goals"): prof_lines.append(f"цели: {profile['goals']}")
    prof_text = "; ".join(prof_lines)
    profile_hint = (
        f"Учитывай профиль пользователя ({prof_text}). "
        "Если визуально на фото есть расхождения с профилем — отметь это деликатно."
        if prof_text else
        "Если сможешь — уточняй при необходимости данные профиля пользователя (возраст, тип кожи/волос, цели)."
    )

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
    return (
        f"{common}\n{profile_hint}\n\n"
        "Форматируй ответ в HTML (теги <b>, <i>, переносы строк).\n\n"
        f"{specific}"
    )

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
        build_prompt(mode, get_profile(user_data)),
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]

    try:
        response = model.generate_content(payload)
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            text = "Ответ пустой. Возможно, сработала модерация или сбой."
        if len(text) > 1800:
            text = text[:1800] + "\n\n<i>Сокращено. Напиши /help для подсказок.</i>"

        # --- Надёжная отправка: сначала HTML, при ошибке — plain text ---
        try:
            await chat.send_message(
                text,
                parse_mode="HTML",
                reply_markup=action_keyboard(),
                disable_web_page_preview=True,
            )
        except BadRequest as e:
            log.warning("HTML parse failed (%s). Fallback to plain text.", e)
            safe = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)  # <br> -> \n
            safe = re.sub(r"(?i)</?(p|div|ul|ol|li|h[1-6])[^>]*>", "\n", safe)
            safe = re.sub(r"<[^>]+>", "", safe)             # снять прочие теги
            safe = re.sub(r"\n{3,}", "\n\n", safe).strip()
            if not safe:
                safe = "Не удалось отформатировать ответ.\n\n" + text
            await chat.send_message(
                safe,
                reply_markup=action_keyboard(),
                disable_web_page_preview=True,
            )
    except Exception as e:
        log.exception("Gemini error")
        await chat.send_message(f"Ошибка анализа изображения: {e}")

# ---------- КОМАНДЫ / CALLBACK-и / FALLBACK ТЕКСТА ----------
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["welcomed"] = True
    await send_home(update.effective_chat, context.user_data)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("welcomed"):
        context.user_data["welcomed"] = True
        return await send_home(update.effective_chat, context.user_data)
    current = get_mode(context.user_data)
    await update.message.reply_text(
        "Я жду фото 🙂\nМожно сменить режим: /mode или отредактируй 🧑‍💼 Профиль",
        reply_markup=mode_keyboard(current)
    )

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
        "ℹ️ Это не медицинская консультация.\n"
        "Для тонкой настройки используй /profile.",
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

# ---------- ПРОФИЛЬ: ConversationHandler ----------
async def profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Давай настроим профиль ✨\n\n"
        "Сколько тебе лет? (число, например 25)\n\n"
        "Команда /cancel — в любой момент отменить."
    )
    return P_AGE

async def profile_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit() or not (5 <= int(text) <= 100):
        return await update.message.reply_text("Пожалуйста, введи возраст числом от 5 до 100.")
    get_profile(context.user_data)["age"] = int(text)
    await update.message.reply_text(
        "Отлично! Теперь опиши тип кожи (например: нормальная/жирная/сухая/комбинированная, чувствительная/нет):"
    )
    return P_SKIN

async def profile_skin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_profile(context.user_data)["skin"] = (update.message.text or "").strip()[:100]
    await update.message.reply_text(
        "Принято. Какой тип/состояние волос? (например: тонкие, окрашенные, склонны к жирности, чувствительная кожа головы)"
    )
    return P_HAIR

async def profile_hair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_profile(context.user_data)["hair"] = (update.message.text or "").strip()[:120]
    await update.message.reply_text(
        "Последний шаг: какие у тебя цели/предпочтения? (например: меньше блеска, объём, мягкое очищение, без сульфатов)"
    )
    return P_GOALS

async def profile_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_profile(context.user_data)["goals"] = (update.message.text or "").strip()[:160]
    txt = profile_to_text(get_profile(context.user_data))
    await update.message.reply_text(
        f"Готово! Твой профиль:\n\n{txt}\n\n"
        "Теперь пришли фото — учту эти данные при анализе.",
    )
    return ConversationHandler.END

async def profile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отменил настройку профиля. Можно вернуться позже: /profile")
    return ConversationHandler.END

async def myprofile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Текущий профиль:\n\n" + profile_to_text(get_profile(context.user_data)))

# ---------- CALLBACK-и ----------
async def on_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = (q.data or "").strip()

    # Фидбек
    if data in ("fb:up", "fb:down"):
        await q.answer("Спасибо за отзыв!" if data == "fb:up" else "Принято 👍", show_alert=False)
        log.info("Feedback from %s: %s", update.effective_user.id, data)
        return

    # Домой
    if data == "home":
        await q.answer()
        return await send_home(update.effective_chat, context.user_data)

    # Меню режимов
    if data == "mode_menu":
        await q.answer()
        current = get_mode(context.user_data)
        return await q.edit_message_text(
            f"Текущий режим: {MODES[current]}\nВыбери другой:",
            reply_markup=mode_keyboard(current)
        )

    # Переключение режима
    if data.startswith("mode:"):
        await q.answer("Режим обновлён")
        mode = data.split(":", 1)[1]
        set_mode(context.user_data, mode)
        return await q.edit_message_text(
            f"Режим установлен: {MODES[mode]}\nПришли фото.",
            reply_markup=mode_keyboard(mode)
        )

    # Профиль (кнопка)
    if data == "profile":
        await q.answer()
        await q.message.reply_text("Открываю мастер настройки профиля…")
        # Запускаем через команду, чтобы ConversationHandler перехватил
        return await profile_start(update.to_dict()["callback_query"]["message"], context)

# ---------- ОБРАБОТЧИКИ ИЗОБРАЖЕНИЙ ----------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # rate limit
    user_id = update.effective_user.id
    now = time.time()
    last = LAST_ANALYSIS_AT.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        wait = int(max(1, RATE_LIMIT_SECONDS - (now - last)))
        return await update.message.reply_text(
            f"Подожди {wait} сек. перед следующим анализом ⏳",
            reply_markup=mode_keyboard(get_mode(context.user_data))
        )
    LAST_ANALYSIS_AT[user_id] = now

    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data), context.user_data)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # rate limit
    user_id = update.effective_user.id
    now = time.time()
    last = LAST_ANALYSIS_AT.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        wait = int(max(1, RATE_LIMIT_SECONDS - (now - last)))
        return await update.message.reply_text(
            f"Подожди {wait} сек. перед следующим анализом ⏳",
            reply_markup=mode_keyboard(get_mode(context.user_data))
        )

    doc = update.message.document
    if not (doc and doc.mime_type and doc.mime_type.startswith("image/")):
        return await update.message.reply_text("Пришли, пожалуйста, фото.")

    LAST_ANALYSIS_AT[user_id] = now
    file = await doc.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data), context.user_data)

# ---------- АВТОПРИВЕТ ----------
async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member.new_chat_member.status == "member":
        context.user_data["welcomed"] = True
        await send_home(update.effective_chat, context.user_data)

# ---------- ОШИБКИ ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Dispatcher error: %s", context.error)

# ---------- HEALTHZ (Flask) ----------
def start_flask_healthz(port: int):
    """/healthz — для Render Health Check (HTTP 200)."""
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    th = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False))
    th.daemon = True
    th.start()
    log.info("Flask /healthz running on port %s", port)

# ---------- MAIN (форс-polling) ----------
def main() -> None:
    tg_app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    tg_app.add_handler(CommandHandler("start", on_start))
    tg_app.add_handler(CommandHandler("mode", on_mode))
    tg_app.add_handler(CommandHandler("help", on_help))
    tg_app.add_handler(CommandHandler("privacy", on_privacy))
    tg_app.add_handler(CommandHandler("myprofile", myprofile))

    # Профиль: диалог
    profile_conv = ConversationHandler(
        entry_points=[CommandHandler("profile", profile_start)],
        states={
            P_AGE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_age)],
            P_SKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_skin)],
            P_HAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_hair)],
            P_GOALS:[MessageHandler(filters.TEXT & ~filters.COMMAND, profile_goals)],
        },
        fallbacks=[CommandHandler("cancel", profile_cancel)],
        name="profile_conv",
        persistent=False,
    )
    tg_app.add_handler(profile_conv)

    # Кнопки
    tg_app.add_handler(CallbackQueryHandler(on_mode_callback))  # принимаем home/mode/fb/profile

    # Сообщения
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Автопривет по Start
    tg_app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    tg_app.add_error_handler(on_error)

    # Всегда поднимаем healthz и ВСЕГДА запускаем polling
    start_flask_healthz(PORT)
    logging.warning("Force POLLING mode (WEBHOOK_URL игнорируется)")
    tg_app.run_polling()

if __name__ == "__main__":
    main()
