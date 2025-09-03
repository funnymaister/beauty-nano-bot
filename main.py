import os
import io
import re
import time
import json
import base64
import asyncio
import logging
from datetime import datetime
from threading import Thread
from typing import Dict, Any, List

from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("gspread").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ---------- КОНФИГ ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
DEFAULT_FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))
DEFAULT_PRICE_RUB = int(os.getenv("PRICE_RUB", "299"))

# Перфоманс
IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "896"))

# Хранилище файлов (история/индексы)
DATA_DIR = os.getenv("DATA_DIR", "./data")
HISTORY_ENABLED = os.getenv("HISTORY_ENABLED", "1") == "1"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))

# Google Sheets
SHEETS_ENABLED = os.getenv("SHEETS_ENABLED", "1") == "1"
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
SERVICE_JSON_B64 = os.getenv("GOOGLE_SHEETS_CREDS")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY")

# ---------- ФАЙЛЫ ДАННЫХ ----------
os.makedirs(DATA_DIR, exist_ok=True)
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
USAGE_FILE = os.path.join(DATA_DIR, "usage.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback.json")

# История (индекс + файлы)
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
HISTORY_DIR  = os.path.join(DATA_DIR, "history")
os.makedirs(HISTORY_DIR, exist_ok=True)

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

# ---------- АДМИНЫ (исправлено) ----------
def parse_admin_ids(val: str | None) -> set[int]:
    """ADMIN_IDS в .env: '123,456; 789' -> {123,456,789}"""
    if not val:
        return set()
    raw = val.replace(";", ",")
    ids = set()
    for p in raw.split(","):
        p = p.strip()
        if p.isdigit():
            ids.add(int(p))
    return ids

seed_admins: set[int] = parse_admin_ids(os.getenv("ADMIN_IDS"))

# всегда читаем файл, затем ДОБАВЛЯЕМ seed из ENV и сохраняем
ADMINS: set[int] = set(load_json(ADMINS_FILE, []))
if seed_admins:
    ADMINS |= seed_admins
save_json(ADMINS_FILE, list(ADMINS))

USERS: set[int] = set(load_json(USERS_FILE, []))
USAGE: Dict[int, Dict[str, Any]] = {int(k): v for k, v in load_json(USAGE_FILE, {}).items()}
CONFIG: Dict[str, Any] = load_json(CONFIG_FILE, {"FREE_LIMIT": DEFAULT_FREE_LIMIT, "PRICE_RUB": DEFAULT_PRICE_RUB})
FEEDBACK: Dict[str, int] = load_json(FEEDBACK_FILE, {"up": 0, "down": 0})
HISTORY: Dict[str, List[Dict[str, Any]]] = load_json(HISTORY_FILE, {})

def persist_all():
    save_json(ADMINS_FILE, list(ADMINS))
    save_json(USERS_FILE, list(USERS))
    save_json(USAGE_FILE, USAGE)
    save_json(CONFIG_FILE, CONFIG)
    save_json(FEEDBACK_FILE, FEEDBACK)
    save_json(HISTORY_FILE, HISTORY)

# ---------- GEMINI ----------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------- GOOGLE SHEETS ----------
_gc = None
_sh = None

def _ensure_ws(title: str, headers: List[str]):
    try:
        ws = _sh.worksheet(title)
        return ws
    except gspread.WorksheetNotFound:
        ws = _sh.add_worksheet(title=title, rows="200", cols=str(max(20, len(headers) + 5)))
        ws.append_row(headers)
        return ws

def sheets_init():
    global _gc, _sh
    if not SHEETS_ENABLED:
        log.info("Sheets disabled by env")
        return
    if not SPREADSHEET_ID or not SERVICE_JSON_B64:
        log.warning("Sheets env missing: GOOGLE_SHEETS_SPREADSHEET_ID or GOOGLE_SHEETS_CREDS")
        return
    try:
        creds_info = json.loads(base64.b64decode(SERVICE_JSON_B64))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
        _gc = gspread.authorize(credentials)
        _sh = _gc.open_by_key(SPREADSHEET_ID)
        _ensure_ws("users",    ["ts", "user_id", "username", "is_admin", "premium"])
        _ensure_ws("analyses", ["ts", "user_id", "username", "mode", "premium", "free_used", "text"])
        _ensure_ws("feedback", ["ts", "user_id", "value"])
        log.info("Sheets connected")
    except Exception as e:
        log.exception("Sheets init failed: %s", e)

def sheets_log_user(user_id: int, username: str | None):
    if not _sh: return
    try:
        ws = _sh.worksheet("users")
        ws.append_row(
            [int(time.time()), user_id, username or "", bool(user_id in ADMINS), bool(usage_entry(user_id).get("premium"))],
            value_input_option="USER_ENTERED"
        )
    except Exception as e:
        log.warning("sheets_log_user failed: %s", e)

def sheets_log_analysis(user_id: int, username: str | None, mode: str, text: str):
    if not _sh: return
    try:
        u = usage_entry(user_id)
        ws = _sh.worksheet("analyses")
        ws.append_row(
            [int(time.time()), user_id, username or "", mode, bool(u.get("premium")), int(u.get("count", 0)), text[:10000]],
            value_input_option="USER_ENTERED"
        )
    except Exception as e:
        log.warning("sheets_log_analysis failed: %s", e)

def sheets_log_feedback(user_id: int, value: str):
    if not _sh: return
    try:
        ws = _sh.worksheet("feedback")
        ws.append_row([int(time.time()), user_id, value], value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning("sheets_log_feedback failed: %s", e)

# ---------- ПАМЯТЬ ----------
LAST_ANALYSIS_AT: Dict[int, float] = {}

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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("face"), callback_data="mode:face"),
         InlineKeyboardButton(label("hair"), callback_data="mode:hair")],
        [InlineKeyboardButton(label("both"), callback_data="mode:both")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
    ])

# ---------- ПРОФИЛЬ (Conversation) ----------
P_AGE, P_SKIN, P_HAIR, P_GOALS = range(4)
def get_profile(user_data: dict) -> Dict[str, Any]:
    return user_data.setdefault("profile", {})
def profile_to_text(pr: Dict[str, Any]) -> str:
    if not pr: return "Профиль пуст."
    parts = []
    if pr.get("age"): parts.append(f"Возраст: {pr['age']}")
    if pr.get("skin"): parts.append(f"Кожа: {pr['skin']}")
    if pr.get("hair"): parts.append(f"Волосы: {pr['hair']}")
    if pr.get("goals"): parts.append(f"Цели: {pr['goals']}")
    return "\n".join(parts)

async def profile_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сколько тебе лет? (число 5–100)")
    return P_AGE

async def profile_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Сколько тебе лет? (число 5–100)")
    return P_AGE

async def profile_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if not t.isdigit() or not (5 <= int(t) <= 100):
        return await update.message.reply_text("Введи возраст числом 5–100.")
    get_profile(context.user_data)["age"] = int(t)
    await update.message.reply_text("Опиши тип кожи (например: нормальная/жирная/сухая/комби; чувствительная/нет):")
    return P_SKIN

async def profile_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["skin"] = (update.message.text or "").strip()[:100]
    await update.message.reply_text("Какой тип/состояние волос? (пример: тонкие, окрашенные, склонны к жирности...)")
    return P_HAIR

async def profile_hair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["hair"] = (update.message.text or "").strip()[:120]
    await update.message.reply_text("Твои цели/предпочтения? (пример: меньше блеска, объём, мягкое очищение...)")
    return P_GOALS

async def profile_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["goals"] = (update.message.text or "").strip()[:160]
    await update.message.reply_text("Готово! Профиль сохранён:\n\n" + profile_to_text(get_profile(context.user_data)))
    return ConversationHandler.END

async def profile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, отменил настройку профиля. /profile — начать заново.")
    return ConversationHandler.END

# ---------- ХЕЛПЕРЫ ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS
def ensure_user(user_id: int):
    if user_id not in USERS:
        USERS.add(user_id); persist_all()

def usage_entry(user_id: int) -> Dict[str, Any]:
    now = datetime.utcnow(); m = now.month
    u = USAGE.setdefault(user_id, {"count": 0, "month": m, "premium": False})
    if u.get("month") != m:
        u["count"] = 0; u["month"] = m
    return u

def check_usage(user_id: int) -> bool:
    u = usage_entry(user_id)
    if u.get("premium"): return True
    limit = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    if u["count"] < limit:
        u["count"] += 1; persist_all(); return True
    return False

def get_usage_text(user_id: int) -> str:
    u = usage_entry(user_id)
    if u.get("premium"): return "🌟 У тебя активен Премиум (безлимит анализов)."
    limit = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    left = max(0, limit - u["count"])
    return f"Осталось бесплатных анализов в этом месяце: {left} из {limit}."

# ---------- ФИЛЬТР: убрать советы про фото ----------
PHOTO_TIPS_PATTERNS = [
    r"улучш(ить|ения?)\s+(качества|фото|изображения)",
    r"качество\s+(фото|изображения)",
    r"освещени[ея]",
    r"ракурс",
    r"(камера|объектив|смартфон|зеркалк)",
    r"сделай(те)?\s+фото",
    r"пересним(и|ите)",
    r"перефотографируй(те)?",
    r"фон.*(равномерн|однотонн)",
    r"резкост[ьи]",
    r"шум(ы)?\s+на\s+фото",
    r"неч[её]тк(о|ость)|размыто",
    r"увеличь(те)?\s+разрешение",
]
_photo_tips_rx = re.compile("|".join(PHOTO_TIPS_PATTERNS), re.IGNORECASE | re.UNICODE)

def remove_photo_tips(text: str) -> str:
    parts = re.split(r"\n{2,}", (text or "").strip())
    kept = []
    for p in parts:
        if _photo_tips_rx.search(p):
            continue
        kept.append(p)
    result = "\n\n".join(kept).strip()
    return result or text

# ---------- История ----------
def _hist_user_dir(uid: int) -> str:
    p = os.path.join(HISTORY_DIR, str(uid))
    os.makedirs(p, exist_ok=True)
    return p

def save_history(uid: int, mode: str, jpeg_bytes: bytes, text: str) -> None:
    if not HISTORY_ENABLED:
        return
    try:
        ts = int(time.time())
        udir = _hist_user_dir(uid)
        img_path = os.path.join(udir, f"{ts}.jpg")
        txt_path = os.path.join(udir, f"{ts}.txt")
        with open(img_path, "wb") as f:
            f.write(jpeg_bytes)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        key = str(uid)
        items = HISTORY.get(key, [])
        items.append({"ts": ts, "mode": mode, "img": img_path, "txt": txt_path})
        items = sorted(items, key=lambda x: x["ts"], reverse=True)[:HISTORY_LIMIT]
        HISTORY[key] = items
        persist_all()
    except Exception as e:
        log.warning("history save failed: %s", e)

def list_history(uid: int) -> List[Dict[str, Any]]:
    return HISTORY.get(str(uid), [])

def history_keyboard(uid: int) -> InlineKeyboardMarkup:
    entries = list_history(uid)
    if not entries:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="home")]])
    rows: List[List[InlineKeyboardButton]] = []
    for e in entries[:10]:
        dt = datetime.fromtimestamp(e["ts"]).strftime("%d.%m %H:%M")
        title = f"{dt} • {MODES.get(e.get('mode','both'),'')}"
        rows.append([InlineKeyboardButton(title, callback_data=f"hist:{e['ts']}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)

# ---------- КЛАВИАТУРЫ ----------
def action_keyboard(for_user_id: int, user_data: dict | None = None) -> InlineKeyboardMarkup:
    premium = usage_entry(for_user_id).get("premium", False)
    buttons = [
        [InlineKeyboardButton("🔄 Новый анализ", callback_data="home")],
        [InlineKeyboardButton("⚙️ Режим", callback_data="mode_menu")],
        [InlineKeyboardButton("🧑‍💼 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🗂 История", callback_data="history")],
        [InlineKeyboardButton("👍 Полезно", callback_data="fb:up"),
         InlineKeyboardButton("👎 Не очень", callback_data="fb:down")],
        [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]
    ]
    if not premium:
        buttons.append([InlineKeyboardButton("🌟 Премиум", callback_data="premium")])
    else:
        buttons.append([InlineKeyboardButton("💳 Купить снова (продлить)", callback_data="renew")])
    if for_user_id and is_admin(for_user_id):
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

# ---------- HELPER: run blocking ----------
async def run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# ---------- АНАЛИЗ ----------
async def _process_image_bytes(chat, img_bytes: bytes, mode: str, user_data: dict, user_id: int, username: str | None):
    ensure_user(user_id)
    if not check_usage(user_id):
        return await chat.send_message(
            "🚫 Лимит бесплатных анализов исчерпан.\n\nОформи 🌟 Премиум (безлимит):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌟 Купить Премиум", callback_data="premium")],
                [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]
            ])
        )
    try:
        def _prep(img_bytes: bytes) -> bytes:
            im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue()
        jpeg_bytes = await run_blocking(_prep, img_bytes)
    except Exception:
        log.exception("PIL convert error")
        return await chat.send_message("Не удалось обработать фото. Попробуй другое.")

    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
    payload = [
        (
            "Ты бьюти-ассистент. Проанализируй фото в контексте режима: "
            f"{mode}. Дай чёткие, практичные рекомендации по уходу/стайлингу/"
            "подбору средств (названия классов ингредиентов, форм-факторов), "
            "структурируй списками и блоками.\n\n"
            "ВАЖНО: НЕ давай никаких советов по улучшению качества фотографии, "
            "освещению, ракурсу, камере и т.п. Никаких фраз про «переснять/сделать фото заново»."
            " Оцени и советуй ИСКЛЮЧИТЕЛЬНО по уходу и продуктам."
        ),
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]
    try:
        response = await run_blocking(model.generate_content, payload)
        text = (getattr(response, "text", "") or "").strip() or "Ответ пустой."
        text = remove_photo_tips(text)
        if len(text) > 1800:
            text = text[:1800] + "\n\n<i>Сокращено.</i>"

        async def _save_history():
            try:
                await run_blocking(save_history, user_id, mode, jpeg_bytes, text)
            except Exception as e:
                log.warning("history async failed: %s", e)

        async def _log_sheets():
            try:
                sheets_log_analysis(user_id, username, mode, text)
            except Exception as e:
                log.warning("sheets async failed: %s", e)

        asyncio.create_task(_save_history())
        if SHEETS_ENABLED and _sh:
            asyncio.create_task(_log_sheets())

        try:
            await chat.send_message(text, parse_mode="HTML", reply_markup=action_keyboard(user_id, user_data))
        except BadRequest:
            safe = re.sub(r"<[^>]+>", "", text)
            await chat.send_message(safe, reply_markup=action_keyboard(user_id, user_data))

        await chat.send_message(get_usage_text(user_id))
    except Exception as e:
        log.exception("Gemini error")
        await chat.send_message(f"Ошибка анализа: {e}")

# ---------- ОБЩИЕ ХЭНДЛЕРЫ ----------
async def send_home(chat, user_id: int, user_data: dict):
    await chat.send_message(
        "Привет! Я Beauty Nano Bot 💇‍♀️🤖\nПришли фото — дам рекомендации.\nБесплатные анализы каждый месяц.",
        reply_markup=action_keyboard(user_id, user_data)
    )
    await chat.send_message(get_usage_text(user_id))

async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    sheets_log_user(uid, getattr(update.effective_user, "username", None))
    await send_home(update.effective_chat, uid, context.user_data)

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    now = time.time()
    if now - LAST_ANALYSIS_AT.get(uid, 0) < RATE_LIMIT_SECONDS:
        return await update.message.reply_text("Подожди пару секунд ⏳")
    LAST_ANALYSIS_AT[uid] = now
    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO(); await file.download_to_memory(out=buf)
    await _process_image_bytes(
        update.effective_chat, buf.getvalue(),
        get_mode(context.user_data), context.user_data,
        uid, getattr(update.effective_user, "username", None)
    )

# ---------- КНОПКИ ----------
ADMIN_STATE: Dict[int, Dict[str, Any]] = {}

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = (q.data or "").strip()
    uid = update.effective_user.id; ensure_user(uid)

    if data == "home":
        await q.answer()
        return await send_home(update.effective_chat, uid, context.user_data)

    if data == "mode_menu":
        await q.answer()
        current = get_mode(context.user_data)
        return await q.message.reply_text(
            f"Текущий режим: {MODES[current]}\nВыбери другой:", reply_markup=mode_keyboard(current)
        )
    if data.startswith("mode:"):
        await q.answer("Режим обновлён")
        mode = data.split(":", 1)[1]
        set_mode(context.user_data, mode)
        return await q.message.reply_text(
            f"Режим установлен: {MODES[mode]}\nПришли фото.", reply_markup=action_keyboard(uid, context.user_data)
        )

    if data == "history":
        await q.answer()
        items = list_history(uid)
        if not items:
            return await q.message.reply_text(
                "История пуста. Пришли фото — и анализ попадёт сюда.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="home")]])
            )
        return await q.message.reply_text("Твоя история (последние 10):", reply_markup=history_keyboard(uid))

    if data.startswith("hist:"):
        await q.answer()
        ts_str = data.split(":", 1)[1]
        rec = next((r for r in list_history(uid) if str(r["ts"]) == ts_str), None)
        if not rec:
            return await q.message.reply_text("Запись не найдена.", reply_markup=history_keyboard(uid))
        try:
            with open(rec["txt"], "r", encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            txt = "(не удалось прочитать текст)"
        caption = txt[:1024] if txt else f"Режим: {MODES.get(rec.get('mode','both'),'')}"
        try:
            with open(rec["img"], "rb") as ph:
                await q.message.reply_photo(photo=ph, caption=caption)
        except Exception:
            await q.message.reply_text(caption)
        return await q.message.reply_text("Выбери другую запись:", reply_markup=history_keyboard(uid))

    if data == "limits":
        await q.answer()
        return await q.message.reply_text(get_usage_text(uid))

    if data == "premium":
        await q.answer()
        price = int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        return await q.message.reply_text(
            "🌟 <b>Премиум</b>\n\n• Безлимит анализов\n• Экспорт в PDF (скоро)\n• История анализов (скоро)\n\n"
            f"Цена: {price} ₽ / месяц",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Купить", callback_data="buy")],
                                               [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")]])
        )
    if data == "buy":
        u = usage_entry(uid); u["premium"] = True; persist_all()
        await q.answer()
        return await q.message.reply_text("✅ Премиум активирован!", reply_markup=action_keyboard(uid, context.user_data))
    if data == "renew":
        u = usage_entry(uid); u["premium"] = True; persist_all()
        await q.answer("Продлено")
        return await q.message.edit_text("Премиум продлён ✅", reply_markup=action_keyboard(uid, context.user_data))

    if data == "fb:up":
        FEEDBACK["up"] = FEEDBACK.get("up", 0) + 1; persist_all()
        sheets_log_feedback(uid, "up")
        return await q.answer("Спасибо!", show_alert=False)
    if data == "fb:down":
        FEEDBACK["down"] = FEEDBACK.get("down", 0) + 1; persist_all()
        sheets_log_feedback(uid, "down")
        return await q.answer("Принято 👍", show_alert=False)

    if data == "admin":
        if not is_admin(uid):
            return await q.answer("Недостаточно прав", show_alert=True)
        await q.answer()
        return await q.message.reply_text("🛠 Админ-панель", reply_markup=admin_root_kb())

    if data.startswith("admin:"):
        if not is_admin(uid):
            return await q.answer("Недостаточно прав", show_alert=True)
        await q.answer()
        cmd = data.split(":", 1)[1]
        if cmd == "users":
            return await q.message.reply_text("👥 Управление пользователями", reply_markup=admin_users_kb())
        if cmd == "stats":
            total_users = len(USERS)
            premium_users = sum(1 for u in USAGE.values() if u.get("premium"))
            this_month = datetime.utcnow().month
            total_analyses = sum(usage_entry(uid2)["count"] for uid2 in USERS if usage_entry(uid2)["month"] == this_month)
            fb_up = FEEDBACK.get("up", 0); fb_down = FEEDBACK.get("down", 0)
            txt = (f"📊 Статистика:\n• Пользователей: {total_users}\n• Премиум: {premium_users}\n"
                   f"• Анализов (этот месяц): {total_analyses}\n• Фидбек 👍/👎: {fb_up}/{fb_down}\n"
                   f"• FREE_LIMIT: {CONFIG.get('FREE_LIMIT')} • PRICE: {CONFIG.get('PRICE_RUB')} ₽")
            return await q.message.reply_text(txt, reply_markup=admin_root_kb())
        if cmd == "bonus":
            return await q.message.reply_text("🎁 Бонусы/Подарки", reply_markup=admin_bonus_kb())
        if cmd == "settings":
            return await q.message.reply_text("⚙️ Настройки", reply_markup=admin_settings_kb())
        if cmd == "broadcast":
            ADMIN_STATE[uid] = {"mode": "broadcast"}
            return await q.message.reply_text("Введи текст рассылки (одним сообщением).")
        if cmd in ("add_admin", "rem_admin", "grant_premium", "revoke_premium", "add_free", "user_info"):
            ADMIN_STATE[uid] = {"mode": cmd}
            prompts = {
                "add_admin": "Отправь user_id нового администратора (или перешли его сообщение).",
                "rem_admin": "Отправь user_id администратора для снятия.",
                "grant_premium": "Отправь user_id, кому выдать Премиум.",
                "revoke_premium": "Отправь user_id, у кого снять Премиум.",
                "add_free": "Формат: user_id пробел количество (пример: 123456 3).",
                "user_info": "Отправь user_id пользователя.",
            }
            return await q.message.reply_text(prompts[cmd], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:users")]]))
        if cmd == "set_limit":
            ADMIN_STATE[uid] = {"mode": "set_limit"}
            return await q.message.reply_text(f"Текущий FREE_LIMIT={CONFIG.get('FREE_LIMIT')}. Введи новое целое число.",
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:settings")]]))
        if cmd == "set_price":
            ADMIN_STATE[uid] = {"mode": "set_price"}
            return await q.message.reply_text(f"Текущая цена={CONFIG.get('PRICE_RUB')} ₽. Введи новую цену (целое).",
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:settings")]]))

# ---------- АДМИН: текстовые режимы ----------
def extract_user_id_from_message(update: Update) -> int | None:
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id
    if update.message and update.message.forward_from:
        return update.message.forward_from.id
    if update.message and update.message.text:
        parts = update.message.text.strip().split()
        if parts and parts[0].isdigit():
            return int(parts[0])
    return None

async def on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        return
    st = ADMIN_STATE.get(admin_id)
    if not st:
        return
    mode = st.get("mode")

    if mode == "broadcast":
        text = update.message.text or ""
        sent, failed = 0, 0
        for uid in list(USERS):
            try:
                await context.bot.send_message(uid, f"📣 Сообщение от администратора:\n\n{text}")
                sent += 1
            except (Forbidden, Exception):
                failed += 1
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"Готово. Успешно: {sent}, ошибок: {failed}.", reply_markup=admin_root_kb())

    if mode in ("add_admin", "rem_admin", "grant_premium", "revoke_premium", "user_info"):
        target_id = extract_user_id_from_message(update)
        if not target_id:
            return await update.message.reply_text("Не смог распознать user_id.")
        ensure_user(target_id)
        if mode == "add_admin":
            ADMINS.add(target_id); persist_all()
            return await update.message.reply_text(f"✅ {target_id} назначен админом.", reply_markup=admin_users_kb())
        if mode == "rem_admin":
            if target_id in ADMINS:
                ADMINS.remove(target_id); persist_all()
                return await update.message.reply_text(f"✅ {target_id} снят с админов.", reply_markup=admin_users_kb())
            return await update.message.reply_text("Этот пользователь не админ.", reply_markup=admin_users_kb())
        if mode == "grant_premium":
            u = usage_entry(target_id); u["premium"] = True; persist_all()
            return await update.message.reply_text(f"✅ Выдал Премиум {target_id}.", reply_markup=admin_users_kb())
        if mode == "revoke_premium":
            u = usage_entry(target_id); u["premium"] = False; persist_all()
            return await update.message.reply_text(f"✅ Снял Премиум у {target_id}.", reply_markup=admin_users_kb())
        if mode == "user_info":
            u = usage_entry(target_id)
            txt = (f"ℹ️ Пользователь {target_id}\n• Премиум: {'да' if u.get('premium') else 'нет'}\n"
                   f"• Анализов (этот месяц): {u.get('count', 0)} / лимит {CONFIG.get('FREE_LIMIT')}\n"
                   f"• Месяц записи: {u.get('month')}\n• Известен боту: {'да' if target_id in USERS else 'нет'}\n"
                   f"• Админ: {'да' if target_id in ADMINS else 'нет'}")
            return await update.message.reply_text(txt, reply_markup=admin_users_kb())

    if mode == "add_free":
        text = (update.message.text or "").strip()
        parts = text.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return await update.message.reply_text("Формат: user_id количество (пример: 123456 3)")
        target_id = int(parts[0]); add_n = int(parts[1])
        ensure_user(target_id)
        u = usage_entry(target_id)
        u["count"] = max(0, u.get("count", 0) - add_n)
        persist_all()
        return await update.message.reply_text(
            f"✅ Добавил {add_n} анализов пользователю {target_id}. Текущее использовано: {u['count']}.",
            reply_markup=admin_users_kb()
        )

    if mode == "set_limit":
        txt = (update.message.text or "").strip()
        if not txt.isdigit():
            return await update.message.reply_text("Введи целое число.")
        CONFIG["FREE_LIMIT"] = int(txt); persist_all()
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"✅ FREE_LIMIT обновлён: {CONFIG['FREE_LIMIT']}", reply_markup=admin_settings_kb())

    if mode == "set_price":
        txt = (update.message.text or "").strip()
        if not txt.isdigit():
            return await update.message.reply_text("Введи целую цену (₽).")
        CONFIG["PRICE_RUB"] = int(txt); persist_all()
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"✅ Цена обновлена: {CONFIG['PRICE_RUB']} ₽", reply_markup=admin_settings_kb())

# ---------- КОМАНДЫ СЕРВИСНЫЕ ----------
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"Твой user_id: <code>{u.id}</code>", parse_mode="HTML")

async def cmd_make_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назначить админа: доступно только тем, кто указан в ADMIN_IDS (seed_admins)."""
    caller_id = update.effective_user.id
    if caller_id not in seed_admins:
        return await update.message.reply_text("Недостаточно прав.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Использование: /make_admin <user_id>")
    target = int(context.args[0])
    ADMINS.add(target)
    persist_all()
    await update.message.reply_text(f"✅ Пользователь {target} назначен администратором.")

async def on_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def on_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS:
        return await update.message.reply_text("Недостаточно прав.")
    total_users = len(USERS)
    premium_users = sum(1 for u in USAGE.values() if u.get("premium"))
    limit = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    price = int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))

    hist_path = os.path.abspath(HISTORY_DIR)
    hist_ok = True
    try:
        if HISTORY_ENABLED:
            test_file = os.path.join(HISTORY_DIR, ".wtest")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
    except Exception:
        hist_ok = False

    txt = (
        "<b>Диагностика</b>\n"
        f"• Users: {total_users}\n"
        f"• Premium: {premium_users}\n"
        f"• FREE_LIMIT: {limit}\n"
        f"• PRICE: {price} ₽\n"
        f"• History: {'on' if HISTORY_ENABLED else 'off'}\n"
        f"• History dir: {hist_path}\n"
        f"• Writable: {'yes' if hist_ok else 'no'}\n"
        f"• DATA_DIR: {os.path.abspath(DATA_DIR)}\n"
        f"• Sheets: {'connected' if _sh else 'off'}\n"
    )
    await update.message.reply_text(txt, parse_mode="HTML")

# ---------- HEALTHZ ----------
def start_flask_healthz(port: int):
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    th = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False))
    th.daemon = True
    th.start()
    log.info("Flask /healthz running on port %s", port)

# ---------- MAIN ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    profile_conv = ConversationHandler(
        entry_points=[CommandHandler("profile", profile_start_cmd),
                      CallbackQueryHandler(profile_start_cb, pattern="^profile$")],
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
    app.add_handler(profile_conv)

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("ping", on_ping))
    app.add_handler(CommandHandler("diag", on_diag))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("make_admin", cmd_make_admin))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_text))

    start_flask_healthz(PORT)
    sheets_init()
    app.run_polling()

if __name__ == "__main__":
    main()
