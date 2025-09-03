import os
import io
import re
import time
import json
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
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, ConversationHandler, filters
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
DEFAULT_FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))     # дефолтный лимит/мес.
DEFAULT_PRICE_RUB = int(os.getenv("PRICE_RUB", "299"))     # цена премиума

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY")

# ---------- ФАЙЛЫ ДАННЫХ (простая JSON-персистентность) ----------
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
USAGE_FILE = os.path.join(DATA_DIR, "usage.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback.json")

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Can't save %s: %s", path, e)

# seed админов из ENV (через запятую)
seed_admins = set()
if os.getenv("ADMIN_IDS"):
    try:
        seed_admins = set(int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",") if x.strip().isdigit())
    except Exception:
        pass

ADMINS: set[int] = set(load_json(ADMINS_FILE, list(seed_admins)))
if not ADMINS and seed_admins:
    ADMINS = set(seed_admins)
save_json(ADMINS_FILE, list(ADMINS))

USERS: set[int] = set(load_json(USERS_FILE, []))
USAGE: Dict[int, Dict[str, Any]] = {int(k): v for k, v in load_json(USAGE_FILE, {}).items()}
CONFIG: Dict[str, Any] = load_json(CONFIG_FILE, {"FREE_LIMIT": DEFAULT_FREE_LIMIT, "PRICE_RUB": DEFAULT_PRICE_RUB})
FEEDBACK: Dict[str, int] = load_json(FEEDBACK_FILE, {"up": 0, "down": 0})

def persist_all():
    save_json(ADMINS_FILE, list(ADMINS))
    save_json(USERS_FILE, list(USERS))
    save_json(USAGE_FILE, USAGE)
    save_json(CONFIG_FILE, CONFIG)
    save_json(FEEDBACK_FILE, FEEDBACK)

# ---------- GEMINI ----------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------- ПАМЯТЬ ----------
LAST_ANALYSIS_AT: Dict[int, float] = {}

# ---------- ХЕЛПЕРЫ ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

def ensure_user(user_id: int):
    if user_id not in USERS:
        USERS.add(user_id)
        persist_all()

def usage_entry(user_id: int) -> Dict[str, Any]:
    now = datetime.utcnow()
    m = now.month
    u = USAGE.setdefault(user_id, {"count": 0, "month": m, "premium": False})
    # новый месяц — сброс
    if u.get("month") != m:
        u["count"] = 0
        u["month"] = m
    return u

def check_usage(user_id: int) -> bool:
    u = usage_entry(user_id)
    if u.get("premium"):
        return True
    limit = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    if u["count"] < limit:
        u["count"] += 1
        persist_all()
        return True
    return False

def get_usage_text(user_id: int) -> str:
    u = usage_entry(user_id)
    if u.get("premium"):
        return "🌟 У тебя активен Премиум (безлимит анализов)."
    limit = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    left = max(0, limit - u["count"])
    return f"Осталось бесплатных анализов в этом месяце: {left} из {limit}."

# ---------- КЛАВЫ ----------
def action_keyboard(for_user_id: int) -> InlineKeyboardMarkup:
    premium = usage_entry(for_user_id).get("premium", False)
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
    if is_admin(for_user_id):
        buttons.append([InlineKeyboardButton("🛠 Администратор", callback_data="admin")])
    return InlineKeyboardMarkup(buttons)

def admin_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin:users"),
         InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton("🎁 Бонусы", callback_data="admin:bonus"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="admin:settings")],
        [InlineKeyboardButton("📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
    ])

def admin_users_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Назначить админа", callback_data="admin:add_admin"),
         InlineKeyboardButton("➖ Снять админа", callback_data="admin:rem_admin")],
        [InlineKeyboardButton("🌟 Выдать премиум", callback_data="admin:grant_premium"),
         InlineKeyboardButton("🚫 Снять премиум", callback_data="admin:revoke_premium")],
        [InlineKeyboardButton("➕ Добавить анализы", callback_data="admin:add_free")],
        [InlineKeyboardButton("ℹ️ Инфо по user_id", callback_data="admin:user_info")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin")]
    ])

def admin_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧮 Изменить лимит FREE", callback_data="admin:set_limit")],
        [InlineKeyboardButton("💵 Изменить цену", callback_data="admin:set_price")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin")]
    ])

def admin_bonus_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌟 Выдать премиум", callback_data="admin:grant_premium")],
        [InlineKeyboardButton("➕ Добавить анализы", callback_data="admin:add_free")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin")]
    ])

# ---------- АНАЛИЗ ----------
async def _process_image_bytes(chat, img_bytes: bytes, mode: str, user_data: dict, user_id: int):
    ensure_user(user_id)
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
        f"Ты бьюти-ассистент. Пользователь прислал фото (режим {mode}). Дай структурные рекомендации.",
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]

    try:
        response = model.generate_content(payload)
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            text = "Ответ пустой. Возможно, модерация или сбой."
        if len(text) > 1800:
            text = text[:1800] + "\n\n<i>Сокращено.</i>"

        try:
            await chat.send_message(
                text, parse_mode="HTML", reply_markup=action_keyboard(user_id)
            )
        except BadRequest:
            safe = re.sub(r"<[^>]+>", "", text)
            await chat.send_message(safe, reply_markup=action_keyboard(user_id))

        await chat.send_message(get_usage_text(user_id))
    except Exception as e:
        log.exception("Gemini error")
        await chat.send_message(f"Ошибка анализа: {e}")

# ---------- КОЛБЭКИ ПОЛЬЗОВАТЕЛЕЙ ----------
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    await update.message.reply_text(
        "Привет! Я Beauty Nano Bot 💇‍♀️🤖\n"
        "Пришли фото, а я дам рекомендации.\n"
        "Бесплатные анализы каждый месяц. Хочешь безлимит? Жми 🌟 Премиум.",
        reply_markup=action_keyboard(user_id)
    )
    await update.message.reply_text(get_usage_text(user_id))

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    ensure_user(user_id)
    now = time.time()
    last = LAST_ANALYSIS_AT.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return await update.message.reply_text("Подожди пару секунд ⏳")
    LAST_ANALYSIS_AT[user_id] = now

    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), "both", context.user_data, user_id)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = (q.data or "").strip()
    user_id = update.effective_user.id
    ensure_user(user_id)

    # инфо/лимиты/премиум/фидбек
    if data == "limits":
        await q.answer()
        return await q.message.reply_text(get_usage_text(user_id))

    if data == "premium":
        await q.answer()
        price = int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        return await q.message.reply_text(
            f"🌟 <b>Премиум</b>\n\n"
            f"• Безлимит анализов\n"
            f"• Экспорт в PDF (скоро)\n"
            f"• История анализов (скоро)\n\n"
            f"Цена: {price} ₽ / месяц",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Купить", callback_data="buy")],
                [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]
            ])
        )
    if data == "buy":
        u = usage_entry(user_id)
        u["premium"] = True
        persist_all()
        await q.answer()
        return await q.message.reply_text(
            "✅ Премиум активирован! Теперь у тебя безлимит.",
            reply_markup=action_keyboard(user_id)
        )
    if data == "renew":
        u = usage_entry(user_id)
        u["premium"] = True
        persist_all()
        await q.answer("Премиум продлён")
        return await q.message.edit_text("Премиум продлён ✅", reply_markup=action_keyboard(user_id))

    if data == "fb:up":
        FEEDBACK["up"] = FEEDBACK.get("up", 0) + 1
        persist_all()
        return await q.answer("Спасибо!", show_alert=False)
    if data == "fb:down":
        FEEDBACK["down"] = FEEDBACK.get("down", 0) + 1
        persist_all()
        return await q.answer("Принято 👍", show_alert=False)

    # админ-панель
    if data == "admin":
        if not is_admin(user_id):
            return await q.answer("Недостаточно прав", show_alert=True)
        await q.answer()
        return await q.message.reply_text("🛠 Админ-панель", reply_markup=admin_root_kb())

    if data.startswith("admin:"):
        if not is_admin(user_id):
            return await q.answer("Недостаточно прав", show_alert=True)
        await q.answer()
        cmd = data.split(":", 1)[1]
        if cmd == "users":
            return await q.message.reply_text("👥 Управление пользователями", reply_markup=admin_users_kb())
        if cmd == "stats":
            total_users = len(USERS)
            premium_users = sum(1 for u in USAGE.values() if u.get("premium"))
            this_month = datetime.utcnow().month
            total_analyses = sum(usage_entry(uid)["count"] for uid in USERS if usage_entry(uid)["month"] == this_month)
            fb_up = FEEDBACK.get("up", 0)
            fb_down = FEEDBACK.get("down", 0)
            txt = (
                f"📊 Статистика:\n"
                f"• Пользователей: {total_users}\n"
                f"• Премиум: {premium_users}\n"
                f"• Анализов (этот месяц): {total_analyses}\n"
                f"• Фидбек 👍/👎: {fb_up}/{fb_down}\n"
                f"• Лимит FREE: {CONFIG.get('FREE_LIMIT')} / Цена: {CONFIG.get('PRICE_RUB')} ₽"
            )
            return await q.message.reply_text(txt, reply_markup=admin_root_kb())
        if cmd == "bonus":
            return await q.message.reply_text("🎁 Бонусы/Подарки", reply_markup=admin_bonus_kb())
        if cmd == "settings":
            return await q.message.reply_text("⚙️ Настройки", reply_markup=admin_settings_kb())
        if cmd == "broadcast":
            ADMIN_STATE[user_id] = {"mode": "broadcast"}
            return await q.message.reply_text("Введи текст рассылки (отправь одним сообщением).")

        # пользователи — подкоманды, переводим в режим ожидания ввода
        if cmd in ("add_admin", "rem_admin", "grant_premium", "revoke_premium", "add_free", "user_info"):
            ADMIN_STATE[user_id] = {"mode": cmd}
            prompts = {
                "add_admin": "Отправь user_id нового администратора (или перешли его сообщение).",
                "rem_admin": "Отправь user_id администратора для снятия (или перешли его сообщение).",
                "grant_premium": "Отправь user_id пользователя, которому выдать Премиум (или перешли его сообщение).",
                "revoke_premium": "Отправь user_id пользователя, у которого снять Премиум (или перешли его сообщение).",
                "add_free": "Отправь в формате: user_id пробел количество_добавить (пример: 123456789 3).",
                "user_info": "Отправь user_id пользователя (или перешли его сообщение).",
            }
            return await q.message.reply_text(prompts[cmd], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:users")]]))

        if cmd == "set_limit":
            ADMIN_STATE[user_id] = {"mode": "set_limit"}
            return await q.message.reply_text(f"Текущий FREE_LIMIT={CONFIG.get('FREE_LIMIT')}. Введи новое целое число.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:settings")]]))
        if cmd == "set_price":
            ADMIN_STATE[user_id] = {"mode": "set_price"}
            return await q.message.reply_text(f"Текущая цена={CONFIG.get('PRICE_RUB')} ₽. Введи новую цену (целое).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:settings")]]))

# ---------- СОСТОЯНИЕ АДМИН-РЕЖИМОВ ----------
ADMIN_STATE: Dict[int, Dict[str, Any]] = {}

def extract_user_id_from_message(update: Update) -> int | None:
    # если это ответ на сообщение — берём автора
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id
    # если переслано сообщение — берём оригинального автора
    if update.message and update.message.forward_from:
        return update.message.forward_from.id
    # иначе пытаемся прочитать число из текста
    if update.message and update.message.text:
        parts = update.message.text.strip().split()
        if parts and parts[0].isdigit():
            return int(parts[0])
    return None

async def on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        return  # игнорируем не-админов
    st = ADMIN_STATE.get(admin_id)
    if not st:
        return  # нет активного режима

    mode = st.get("mode")

    # Рассылка
    if mode == "broadcast":
        text = update.message.text or ""
        sent = 0
        failed = 0
        for uid in list(USERS):
            try:
                await context.bot.send_message(uid, f"📣 Сообщение от администратора:\n\n{text}")
                sent += 1
            except Forbidden:
                failed += 1
            except Exception:
                failed += 1
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"Готово. Успешно: {sent}, ошибок: {failed}.", reply_markup=admin_root_kb())

    # Назначить/снять админа
    if mode in ("add_admin", "rem_admin", "grant_premium", "revoke_premium", "user_info"):
        target_id = extract_user_id_from_message(update)
        if not target_id:
            return await update.message.reply_text("Не смог распознать user_id. Перешли сообщение пользователя или пришли его числом.")
        ensure_user(target_id)

        if mode == "add_admin":
            ADMINS.add(target_id)
            persist_all()
            return await update.message.reply_text(f"✅ Пользователь {target_id} назначен администратором.", reply_markup=admin_users_kb())
        if mode == "rem_admin":
            if target_id in ADMINS:
                ADMINS.remove(target_id)
                persist_all()
                return await update.message.reply_text(f"✅ Пользователь {target_id} снят с админов.", reply_markup=admin_users_kb())
            return await update.message.reply_text("Этот пользователь не админ.", reply_markup=admin_users_kb())
        if mode == "grant_premium":
            u = usage_entry(target_id)
            u["premium"] = True
            persist_all()
            return await update.message.reply_text(f"✅ Выдал Премиум пользователю {target_id}.", reply_markup=admin_users_kb())
        if mode == "revoke_premium":
            u = usage_entry(target_id)
            u["premium"] = False
            persist_all()
            return await update.message.reply_text(f"✅ Снял Премиум у пользователя {target_id}.", reply_markup=admin_users_kb())
        if mode == "user_info":
            u = usage_entry(target_id)
            txt = (
                f"ℹ️ Инфо о пользователе {target_id}\n"
                f"• Премиум: {'да' if u.get('premium') else 'нет'}\n"
                f"• Анализов в этом месяце: {u.get('count', 0)} / лимит {CONFIG.get('FREE_LIMIT')}\n"
                f"• Месяц записи: {u.get('month')}\n"
                f"• Известен боту: {'да' if target_id in USERS else 'нет'}\n"
                f"• Админ: {'да' if target_id in ADMINS else 'нет'}"
            )
            return await update.message.reply_text(txt, reply_markup=admin_users_kb())

    # Добавить анализы (уменьшаем счётчик использованных)
    if mode == "add_free":
        text = (update.message.text or "").strip()
        parts = text.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return await update.message.reply_text("Формат: user_id пробел количество (пример: 123456 3)")
        target_id = int(parts[0])
        add_n = int(parts[1])
        ensure_user(target_id)
        u = usage_entry(target_id)
        # добавляем «бесплатных анализов» фактически уменьшая использованное
        u["count"] = max(0, u.get("count", 0) - add_n)
        persist_all()
        return await update.message.reply_text(f"✅ Добавил {add_n} бесплатных анализов пользователю {target_id}. Текущее использовано: {u['count']}.", reply_markup=admin_users_kb())

    # Установка лимита/цены
    if mode == "set_limit":
        txt = (update.message.text or "").strip()
        if not txt.isdigit():
            return await update.message.reply_text("Введи целое число.")
        CONFIG["FREE_LIMIT"] = int(txt)
        persist_all()
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"✅ FREE_LIMIT обновлён: {CONFIG['FREE_LIMIT']}", reply_markup=admin_settings_kb())

    if mode == "set_price":
        txt = (update.message.text or "").strip()
        if not txt.isdigit():
            return await update.message.reply_text("Введи целое число (Цена в ₽).")
        CONFIG["PRICE_RUB"] = int(txt)
        persist_all()
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"✅ Цена обновлена: {CONFIG['PRICE_RUB']} ₽", reply_markup=admin_settings_kb())

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

    # при активном режиме админа — перехватываем текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_text))

    start_flask_healthz(PORT)
    app.run_polling()

if __name__ == "__main__":
    main()
