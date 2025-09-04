import os, io, re, time, json, base64, asyncio, logging
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

# === Google Sheets: справочники как «источник истины»
from refdata import REF

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("beauty-nano-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("gspread").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ---------- КОНФИГ / ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
DEFAULT_FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))
DEFAULT_PRICE_RUB = int(os.getenv("PRICE_RUB", "299"))
IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "896"))

DATA_DIR = os.getenv("DATA_DIR", "./data")
HISTORY_ENABLED = os.getenv("HISTORY_ENABLED", "1") == "1"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))

SHEETS_ENABLED = os.getenv("SHEETS_ENABLED", "1") == "1"
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
SERVICE_JSON_B64 = os.getenv("GOOGLE_SHEETS_CREDS")

if not BOT_TOKEN: raise RuntimeError("Не задан BOT_TOKEN")
if not GEMINI_API_KEY: raise RuntimeError("Не задан GEMINI_API_KEY")

# ---------- ФАЙЛЫ ДАННЫХ ----------
os.makedirs(DATA_DIR, exist_ok=True)
ADMINS_FILE   = os.path.join(DATA_DIR, "admins.json")
USERS_FILE    = os.path.join(DATA_DIR, "users.json")
USAGE_FILE    = os.path.join(DATA_DIR, "usage.json")
CONFIG_FILE   = os.path.join(DATA_DIR, "config.json")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback.json")
HISTORY_FILE  = os.path.join(DATA_DIR, "history.json")
HISTORY_DIR   = os.path.join(DATA_DIR, "history"); os.makedirs(HISTORY_DIR, exist_ok=True)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Can't save %s: %s", path, e)

# ---------- АДМИНЫ ----------
def parse_admin_ids(val: str | None) -> set[int]:
    if not val: return set()
    raw = val.replace(";", ",").replace(" ", ",")
    ids = set()
    for p in raw.split(","):
        p = p.strip()
        if p.isdigit(): ids.add(int(p))
    return ids

seed_admins: set[int] = parse_admin_ids(os.getenv("ADMIN_IDS"))

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
_gc = _sh = None
def _ensure_ws(title: str, headers: List[str]):
    try: return _sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = _sh.add_worksheet(title=title, rows="200", cols=str(max(20, len(headers)+5)))
        ws.append_row(headers); return ws

def sheets_init():
    global _gc, _sh
    if not SHEETS_ENABLED: return
    if not SPREADSHEET_ID or not SERVICE_JSON_B64:
        log.warning("Sheets env missing"); return
    try:
        creds_info = json.loads(base64.b64decode(SERVICE_JSON_B64))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
        _gc = gspread.authorize(credentials); _sh = _gc.open_by_key(SPREADSHEET_ID)
        _ensure_ws("users",    ["ts","user_id","username","is_admin","premium"])
        _ensure_ws("analyses", ["ts","user_id","username","mode","premium","free_used","text"])
        _ensure_ws("feedback", ["ts","user_id","value"])
        log.info("Sheets connected")
    except Exception as e:
        log.exception("Sheets init failed: %s", e)

def sheets_log_user(user_id: int, username: str | None):
    if not _sh: return
    try:
        _sh.worksheet("users").append_row(
            [int(time.time()), user_id, username or "", bool(user_id in ADMINS), bool(USAGE.get(user_id,{}).get("premium"))],
            value_input_option="USER_ENTERED")
    except Exception as e: log.warning("sheets_log_user failed: %s", e)

def sheets_log_analysis(user_id: int, username: str | None, mode: str, text: str):
    if not _sh: return
    try:
        u = USAGE.get(user_id, {})
        _sh.worksheet("analyses").append_row(
            [int(time.time()), user_id, username or "", mode, bool(u.get("premium")), int(u.get("count",0)), text[:10000]],
            value_input_option="USER_ENTERED")
    except Exception as e: log.warning("sheets_log_analysis failed: %s", e)

def sheets_log_feedback(user_id: int, value: str):
    if not _sh: return
    try:
        _sh.worksheet("feedback").append_row([int(time.time()), user_id, value], value_input_option="USER_ENTERED")
    except Exception as e: log.warning("sheets_log_feedback failed: %s", e)

# NEW: тянуть историю из Sheets (analyses)
def sheets_fetch_history(user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Возвращает последние записи анализа для пользователя из листа 'analyses'.
    Структура: {"ts": int, "mode": "face|hair|both", "img": None, "txt_inline": str}
    """
    if not _sh: return []
    try:
        ws = _sh.worksheet("analyses")
        rows = ws.get_all_records(numericise_ignore=["all"])
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                if int(str(r.get("user_id","-1")).strip()) != int(user_id):
                    continue
                ts_raw = str(r.get("ts","")).strip()
                ts = int(ts_raw) if ts_raw.isdigit() else int(time.time())
                mode = (str(r.get("mode","both")) or "both").strip().lower()
                text = (r.get("text") or "").strip()
                out.append({"ts": ts, "mode": mode, "img": None, "txt_inline": text})
            except Exception:
                continue
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out[:limit]
    except Exception as e:
        log.warning("sheets_fetch_history failed: %s", e)
        return []

# ---------- СОСТОЯНИЯ, РЕЖИМЫ, ПРОФИЛЬ ----------
LAST_ANALYSIS_AT: Dict[int, float] = {}

MODES = {"face": "Лицо", "hair": "Волосы", "both": "Лицо + Волосы"}
def get_mode(user_data: dict) -> str: return user_data.get("mode","both")
def set_mode(user_data: dict, mode: str)->None:
    if mode in MODES: user_data["mode"] = mode

def mode_keyboard(active: str) -> InlineKeyboardMarkup:
    def label(key): return f"✅ {MODES[key]}" if key==active else MODES[key]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("face"), callback_data="mode:face"),
         InlineKeyboardButton(label("hair"), callback_data="mode:hair")],
        [InlineKeyboardButton(label("both"), callback_data="mode:both")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
    ])

P_AGE, P_SKIN, P_HAIR, P_GOALS = range(4)
def get_profile(user_data: dict)->Dict[str,Any]: return user_data.setdefault("profile",{})
def profile_to_text(pr: Dict[str,Any])->str:
    if not pr: return "Профиль пуст."
    parts=[]
    if pr.get("age"): parts.append(f"Возраст: {pr['age']}")
    if pr.get("skin"): parts.append(f"Кожа: {pr['skin']}")
    if pr.get("hair"): parts.append(f"Волосы: {pr['hair']}")
    if pr.get("goals"): parts.append(f"Цели: {pr['goals']}")
    return "\n".join(parts)

async def profile_start_cmd(update: Update, _): await update.message.reply_text("Сколько тебе лет? (5–100)"); return P_AGE
async def profile_start_cb(update: Update, _):
    q=update.callback_query; await q.answer(); await q.message.reply_text("Сколько тебе лет? (5–100)"); return P_AGE
async def profile_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t=(update.message.text or "").strip()
    if not t.isdigit() or not (5<=int(t)<=100): return await update.message.reply_text("Введи возраст 5–100.")
    get_profile(context.user_data)["age"]=int(t); await update.message.reply_text("Тип кожи:"); return P_SKIN
async def profile_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["skin"]=(update.message.text or "").strip()[:100]
    await update.message.reply_text("Тип/состояние волос:"); return P_HAIR
async def profile_hair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["hair"]=(update.message.text or "").strip()[:120]
    await update.message.reply_text("Цели/предпочтения:"); return P_GOALS
async def profile_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["goals"]=(update.message.text or "").strip()[:160]
    await update.message.reply_text("Профиль сохранён:\n\n"+profile_to_text(get_profile(context.user_data))); return ConversationHandler.END
async def profile_cancel(update: Update, _): await update.message.reply_text("Отменил. /profile — начать заново."); return ConversationHandler.END

def is_admin(user_id:int)->bool: return user_id in ADMINS
def ensure_user(user_id:int):
    if user_id not in USERS: USERS.add(user_id); persist_all()

def usage_entry(user_id:int)->Dict[str,Any]:
    now=datetime.utcnow(); m=now.month
    u=USAGE.setdefault(user_id, {"count":0,"month":m,"premium":False})
    if u.get("month")!=m: u["count"]=0; u["month"]=m
    return u

def check_usage(user_id:int)->bool:
    u=usage_entry(user_id)
    if u.get("premium"): return True
    limit=int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    if u["count"]<limit: u["count"]+=1; persist_all(); return True
    return False

def get_usage_text(user_id:int)->str:
    u=usage_entry(user_id)
    if u.get("premium"): return "🌟 У тебя активен Премиум (безлимит)."
    limit=int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    left=max(0, limit-u["count"])
    return f"Осталось бесплатных анализов: {left} из {limit}."

# ---------- ФИЛЬТР «не советовать переснимать» ----------
PHOTO_TIPS_PATTERNS=[
    r"улучш(ить|ения?)\s+(качества|фото|изображения)", r"качество\s+(фото|изображения)",
    r"освещени[ея]", r"ракурс", r"(камера|объектив|смартфон|зеркалк)",
    r"сделай(те)?\s+фото", r"пересним(и|ите)", r"перефотографируй(те)?",
    r"фон.*(равномерн|однотонн)", r"резкост[ьи]", r"шум(ы)?\s+на\s+фото",
    r"неч[её]тк(о|ость)|размыто", r"увеличь(те)?\s+разрешение"
]
_photo_tips_rx=re.compile("|".join(PHOTO_TIPS_PATTERNS), re.IGNORECASE|re.UNICODE)
def remove_photo_tips(text:str)->str:
    parts=re.split(r"\n{2,}", (text or "").strip()); kept=[]
    for p in parts:
        if _photo_tips_rx.search(p): continue
        kept.append(p)
    result="\n\n".join(kept).strip()
    return result or text

# ---------- НОВЫЙ СТИЛЬ ОТВЕТА + ДЛИННЫЕ СООБЩЕНИЯ ----------
SAFE_CHUNK = 3500  # запас под HTML/кнопки

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _emoji_bullets(text: str) -> str:
    colors = ["🟢", "🟡", "🔵", "🟣", "🟠"]
    i = 0
    out_lines = []
    for line in text.splitlines():
        if re.match(r"^\s*(?:[•\-\*\u2022]|[0-9]+\.)\s+", line):
            bullet = colors[i % len(colors)]; i += 1
            line = re.sub(r"^\s*(?:[•\-\*\u2022]|[0-9]+\.)\s+", bullet + " ", line)
        line = re.sub(r"\b(утро|утренний)\b", "☀️ утро", line, flags=re.I)
        line = re.sub(r"\b(день|днём|дневной)\b", "🌤️ день", line, flags=re.I)
        line = re.sub(r"\b(вечер|вечерний)\b", "🌙 вечер", line, flags=re.I)
        out_lines.append(line)
    return "\n".join(out_lines)

def _themed_headings(text: str) -> str:
    lines = text.splitlines()
    themed = []
    for ln in lines:
        m = re.match(r"^\s*(утро|день|вечер|ноч[ььи]|ночной|sos|советы|рекомендац(ии|ия))\b[:\-–]?\s*(.*)$", ln, flags=re.I)
        if m:
            key = m.group(1).lower()
            rest = m.group(3)
            emo = "✨"
            if key.startswith("утро"): emo = "☀️"
            elif key.startswith("день"): emo = "🌤️"
            elif key.startswith("вечер"): emo = "🌙"
            elif key.startswith("ноч"): emo = "🌘"
            elif key == "sos": emo = "🚑"
            elif key.startswith("советы") or key.startswith("рекомендац"): emo = "🎯"
            title = key.capitalize()
            ln = f"<b>{emo} {html_escape(title)}</b>"
            if rest: ln += f"\n{html_escape(rest)}"
            themed.append(ln)
        else:
            themed.append(html_escape(ln))
    return "\n".join(themed)

def style_response(raw_text: str, mode: str, profile: dict | None = None) -> str:
    txt = raw_text.strip().replace("\r\n", "\n").replace("\r", "\n")
    txt = _emoji_bullets(txt)
    txt = _themed_headings(txt)
    mode_title = {"face": "Лицо", "hair": "Волосы", "both": "Лицо + Волосы"}.get(mode, "Анализ")
    head = f"<b>💄 Beauty Nano — {mode_title}</b>\n"
    if profile:
        bits = []
        if profile.get("age"):  bits.append(f"{profile['age']} лет")
        if profile.get("skin"): bits.append(profile["skin"])
        if profile.get("hair"): bits.append(profile["hair"])
        if bits: head += f"<i>{html_escape(' / '.join(bits))}</i>\n"
    head += "━━━━━━━━━━━━━━━━\n"
    tail = "\n<i>Готово! Пришли новое фото или измени режим ниже.</i>"
    return head + txt + tail

def _split_chunks(s: str, limit: int = SAFE_CHUNK) -> list[str]:
    s = s.strip()
    parts: list[str] = []
    while len(s) > limit:
        cut = s.rfind("\n\n", 0, limit)
        if cut == -1: cut = s.rfind("\n", 0, limit)
        if cut == -1: cut = limit
        parts.append(s[:cut].strip())
        s = s[cut:].strip()
    if s: parts.append(s)
    return parts

async def send_html_long(chat, html_text: str, keyboard=None):
    chunks = _split_chunks(html_text, SAFE_CHUNK)
    if not chunks: return
    for part in chunks[:-1]:
        try: await chat.send_message(part, parse_mode="HTML")
        except BadRequest: await chat.send_message(re.sub(r"<[^>]+>", "", part))
    last = chunks[-1]
    try: await chat.send_message(last, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest: await chat.send_message(re.sub(r"<[^>]+>", "", last), reply_markup=keyboard)

# ---------- ИСТОРИЯ ----------
def _hist_user_dir(uid:int)->str:
    p=os.path.join(HISTORY_DIR,str(uid)); os.makedirs(p,exist_ok=True); return p
def save_history(uid:int, mode:str, jpeg_bytes:bytes, text:str)->None:
    if not HISTORY_ENABLED: return
    try:
        ts=int(time.time()); udir=_hist_user_dir(uid)
        img=os.path.join(udir,f"{ts}.jpg"); txt=os.path.join(udir,f"{ts}.txt")
        with open(img,"wb") as f: f.write(jpeg_bytes)
        with open(txt,"w",encoding="utf-8") as f: f.write(text)
        key=str(uid); items=HISTORY.get(key,[])
        items.append({"ts":ts,"mode":mode,"img":img,"txt":txt})
        items=sorted(items,key=lambda x:x["ts"],reverse=True)[:HISTORY_LIMIT]
        HISTORY[key]=items; persist_all()
    except Exception as e: log.warning("history save failed: %s", e)

# NEW: смешанный источник истории (локально + Sheets)
def list_history(uid:int)->List[Dict[str,Any]]:
    """
    Смешанный источник:
      - локальные файлы (/data/history)
      - Google Sheets 'analyses' (если доступно)
    Дедуп по ts, сортировка по убыванию.
    """
    local = HISTORY.get(str(uid), [])
    remote = sheets_fetch_history(uid, limit=20) if _sh else []

    norm: List[Dict[str, Any]] = []
    for e in local:
        norm.append({"ts": int(e["ts"]), "mode": e.get("mode","both"), "img": e.get("img"),
                     "txt": e.get("txt"), "txt_inline": None})
    for e in remote:
        norm.append({"ts": int(e["ts"]), "mode": e.get("mode","both"), "img": None,
                     "txt": None, "txt_inline": e.get("txt_inline","")})

    uniq: Dict[int, Dict[str,Any]] = {}
    for e in norm:
        uniq.setdefault(e["ts"], e)  # первым кладём локальный (если был), удалённый перезапишется только если локального нет
    items = sorted(uniq.values(), key=lambda x: x["ts"], reverse=True)
    return items[:HISTORY_LIMIT]

def history_keyboard(uid:int)->InlineKeyboardMarkup:
    entries=list_history(uid)
    if not entries:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад",callback_data="home")]])
    rows=[]
    for e in entries[:10]:
        dt=datetime.fromtimestamp(e["ts"]).strftime("%d.%m %H:%M")
        emoji = "📸"
        mode = MODES.get(e.get("mode","both"),"")
        rows.append([InlineKeyboardButton(f"{emoji} {dt} • {mode}", callback_data=f"hist:{e['ts']}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)

# ---------- КЛАВИАТУРЫ ----------
def action_keyboard(for_user_id:int, user_data:dict|None=None)->InlineKeyboardMarkup:
    premium=usage_entry(for_user_id).get("premium",False)
    buttons=[
        [InlineKeyboardButton("🔄 Новый анализ",callback_data="home")],
        [InlineKeyboardButton("⚙️ Режим",callback_data="mode_menu")],
        [InlineKeyboardButton("🧑‍💼 Профиль",callback_data="profile")],
        [InlineKeyboardButton("🗂 История",callback_data="history")],
        [InlineKeyboardButton("👍 Полезно",callback_data="fb:up"), InlineKeyboardButton("👎 Не очень",callback_data="fb:down")],
        [InlineKeyboardButton("ℹ️ Лимиты",callback_data="limits")]
    ]
    if not premium: buttons.append([InlineKeyboardButton("🌟 Премиум",callback_data="premium")])
    else: buttons.append([InlineKeyboardButton("💳 Продлить премиум",callback_data="renew")])
    if for_user_id and is_admin(for_user_id): buttons.append([InlineKeyboardButton("🛠 Администратор",callback_data="admin")])
    return InlineKeyboardMarkup(buttons)

# ---------- УТИЛИТА ДЛЯ БЛОКИРУЮЩИХ ----------
async def run_blocking(func,*a,**kw): return await asyncio.to_thread(func,*a,**kw)

# ---------- АНАЛИЗ ----------
async def _process_image_bytes(chat, img_bytes:bytes, mode:str, user_data:dict, user_id:int, username:str|None):
    ensure_user(user_id)
    if not check_usage(user_id):
        return await chat.send_message("🚫 Лимит исчерпан. Оформи 🌟 Премиум.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌟 Купить Премиум",callback_data="premium")],
            [InlineKeyboardButton("ℹ️ Лимиты",callback_data="limits")]
        ]))
    try:
        def _prep(b:bytes)->bytes:
            im=Image.open(io.BytesIO(b)).convert("RGB"); im.thumbnail((IMAGE_MAX_SIDE,IMAGE_MAX_SIDE))
            buf=io.BytesIO(); im.save(buf,format="JPEG",quality=85, optimize=True); return buf.getvalue()
        jpeg_bytes=await run_blocking(_prep, img_bytes)
    except Exception:
        log.exception("PIL convert error"); return await chat.send_message("Не удалось обработать фото. Попробуй другое.")

    b64=base64.b64encode(jpeg_bytes).decode("utf-8")
    payload=[
        ("Ты бьюти-ассистент. Проанализируй фото в контексте режима: "
         f"{mode}. Дай чёткие практичные рекомендации по уходу/стайлингу. "
         "Никаких советов про качество фото/освещение/ракурс — только уход и продукты. "
         "Отдельно отметь блоки '☀️ утро', '🌤️ день', '🌙 вечер'."),
        {"inline_data":{"mime_type":"image/jpeg","data":b64}}
    ]
    try:
        response=await run_blocking(model.generate_content, payload)
        text=(getattr(response,"text","") or "").strip() or "Ответ пустой."
        text=remove_photo_tips(text)

        styled = style_response(text, mode, profile=user_data.get("profile"))
        await send_html_long(chat, styled, keyboard=action_keyboard(user_id, user_data))

        async def _save():
            try: await run_blocking(save_history, user_id, mode, jpeg_bytes, text)
            except Exception as e: log.warning("history async failed: %s", e)
        async def _sheets():
            try: sheets_log_analysis(user_id, username, mode, text)
            except Exception as e: log.warning("sheets async failed: %s", e)
        asyncio.create_task(_save())
        if SHEETS_ENABLED and _sh: asyncio.create_task(_sheets())

        await chat.send_message(get_usage_text(user_id))
    except Exception as e:
        log.exception("Gemini error"); await chat.send_message(f"Ошибка анализа: {e}")

# ---------- ОБЩИЕ ХЭНДЛЕРЫ ----------
async def send_home(chat, uid:int, user_data:dict):
    await chat.send_message("Привет! Пришли фото — дам рекомендации.", reply_markup=action_keyboard(uid, user_data))
    await chat.send_message(get_usage_text(uid))

async def on_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; ensure_user(uid)
    sheets_log_user(uid, getattr(update.effective_user,"username",None))
    await send_home(update.effective_chat, uid, context.user_data)
    # Приветствие из листа messages
    try:
        title = REF.msg("welcome_title", "ru", default="Добро пожаловать в Beauty Nano Bot 💄")
        await update.message.reply_text(title)
    except Exception:
        pass

async def on_photo(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; ensure_user(uid)
    now=time.time()
    if now-LAST_ANALYSIS_AT.get(uid,0)<RATE_LIMIT_SECONDS: return await update.message.reply_text("Подожди пару секунд ⏳")
    LAST_ANALYSIS_AT[uid]=now
    file=await update.message.photo[-1].get_file()
    buf=io.BytesIO(); await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data), context.user_data, uid, getattr(update.effective_user,"username",None))

# ---------- КНОПКИ / АДМИНКА ----------
ADMIN_STATE: Dict[int, Dict[str, Any]] = {}

def admin_user_card_kb(target_id:int)->InlineKeyboardMarkup:
    isadm = target_id in ADMINS
    isprem = usage_entry(target_id).get("premium", False)
    rows = [
        [InlineKeyboardButton(("👑 Снять админа" if isadm else "👑 Назначить админом"), callback_data=f"admin:act:{'rem_admin' if isadm else 'add_admin'}:{target_id}")],
        [InlineKeyboardButton(("🚫 Снять премиум" if isprem else "🌟 Выдать премиум"), callback_data=f"admin:act:{'revoke_premium' if isprem else 'grant_premium'}:{target_id}")],
        [InlineKeyboardButton("⬅️ Назад к списку", callback_data="admin:pick_users")]
    ]
    return InlineKeyboardMarkup(rows)

def admin_pick_users_kb()->InlineKeyboardMarkup:
    # последние 12 известных пользователей
    last_ids = sorted(list(USERS))[-12:]
    rows=[]
    for uid in reversed(last_ids):
        flag = ("👑" if uid in ADMINS else "👤")
        star = ("🌟" if usage_entry(uid).get("premium") else "🆓")
        rows.append([InlineKeyboardButton(f"{flag}{star} {uid}", callback_data=f"admin:user:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад",callback_data="admin")])
    return InlineKeyboardMarkup(rows)

async def on_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; data=(q.data or "").strip()
    uid=update.effective_user.id; ensure_user(uid)

    if data=="home": await q.answer(); return await send_home(update.effective_chat, uid, context.user_data)

    if data=="mode_menu":
        await q.answer(); cur=get_mode(context.user_data)
        return await q.message.reply_text(f"Текущий режим: {MODES[cur]}\nВыбери:", reply_markup=mode_keyboard(cur))
    if data.startswith("mode:"):
        await q.answer("Режим обновлён"); m=data.split(":",1)[1]; set_mode(context.user_data,m)
        return await q.message.reply_text(f"Режим установлен: {MODES[m]}\nПришли фото.", reply_markup=action_keyboard(uid, context.user_data))

    if data=="history":
        await q.answer(); items=list_history(uid)
        if not items:
            return await q.message.reply_text("🗂 История пуста.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад",callback_data="home")]]))
        return await q.message.reply_text("🗂 Выбери запись:", reply_markup=history_keyboard(uid))

    if data.startswith("hist:"):
        await q.answer()
        ts = data.split(":",1)[1]
        rec = next((r for r in list_history(uid) if str(r["ts"])==ts), None)
        if not rec:
            return await q.message.reply_text("❌ Запись не найдена.", reply_markup=history_keyboard(uid))

        # читаем полный текст: приоритет — то, что пришло из Sheets
        if rec.get("txt_inline"):
            txt = rec["txt_inline"]
        else:
            try:
                with open(rec["txt"], "r", encoding="utf-8") as f:
                    txt = f.read()
            except Exception:
                txt = "(⚠️ не удалось прочитать текст)"

        dt = datetime.fromtimestamp(rec["ts"]).strftime("%d.%m.%Y %H:%M")
        mode = MODES.get(rec.get("mode", "both"), "Анализ")
        head = f"🗓 {dt}\n🧾 Режим: {mode}\n\n"

        try:
            if rec.get("img"):
                with open(rec["img"], "rb") as ph:
                    await q.message.reply_photo(photo=ph, caption=f"{head}📋 Полный результат ниже ⬇️")
            else:
                await q.message.reply_text(f"{head}📋 Полный результат ниже ⬇️")
        except Exception:
            await q.message.reply_text(f"{head}📋 Полный результат ниже ⬇️")

        for chunk in _split_chunks(txt, SAFE_CHUNK):
            await q.message.reply_text(chunk)

        return await q.message.reply_text("🗂 Выбери запись:", reply_markup=history_keyboard(uid))

    if data=="limits":
        await q.answer()
        daily_free = REF.get_limit("daily_free", CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
        daily_premium = REF.get_limit("daily_premium", 15)
        price_rub = REF.get_price("premium_month_rub", CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        txt = (f"ℹ️ *Лимиты и цена*\n"
               f"— Free: {daily_free} анализов/день\n"
               f"— Premium: {daily_premium} анализов/день\n"
               f"— Цена Premium: {price_rub} ₽/мес")
        return await q.message.reply_text(txt, parse_mode="Markdown")

    if data=="premium":
        await q.answer()
        price=int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        return await q.message.reply_text(
            f"🌟 <b>Премиум</b>\nБезлимит анализов\nЦена: {price} ₽ / месяц",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Купить",callback_data="buy")],[InlineKeyboardButton("ℹ️ Лимиты",callback_data="limits")]]))
    if data=="buy":
        u=usage_entry(uid); u["premium"]=True; persist_all(); await q.answer()
        return await q.message.reply_text("✅ Премиум активирован!", reply_markup=action_keyboard(uid, context.user_data))
    if data=="renew":
        u=usage_entry(uid); u["premium"]=True; persist_all(); await q.answer("Продлено")
        return await q.message.edit_text("Премиум продлён ✅", reply_markup=action_keyboard(uid, context.user_data))

    if data=="fb:up": FEEDBACK["up"]=FEEDBACK.get("up",0)+1; persist_all(); sheets_log_feedback(uid,"up"); return await q.answer("Спасибо!")
    if data=="fb:down": FEEDBACK["down"]=FEEDBACK.get("down",0)+1; persist_all(); sheets_log_feedback(uid,"down"); return await q.answer("Принято")

    if data=="admin":
        if not is_admin(uid): return await q.answer("Недостаточно прав", show_alert=True)
        await q.answer(); return await q.message.reply_text("🛠 Админ-панель", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Пользователи",callback_data="admin:pick_users"),
             InlineKeyboardButton("📊 Статистика",callback_data="admin:stats")],
            [InlineKeyboardButton("🎁 Бонусы",callback_data="admin:bonus"),
             InlineKeyboardButton("⚙️ Настройки",callback_data="admin:settings")],
            [InlineKeyboardButton("📣 Рассылка",callback_data="admin:broadcast")],
            [InlineKeyboardButton("🔄 Обновить справочники",callback_data="admin:reload_refs")],
            [InlineKeyboardButton("⬅️ Назад",callback_data="home")]
        ]))

    if data.startswith("admin:"):
        if not is_admin(uid): return await q.answer("Недостаточно прав", show_alert=True)
        await q.answer(); parts=data.split(":")
        cmd = parts[1]

        if cmd=="pick_users":
            return await q.message.reply_text("👥 Недавние пользователи:", reply_markup=admin_pick_users_kb())

        if cmd=="user" and len(parts)>=3 and parts[2].isdigit():
            target = int(parts[2])
            ensure_user(target)
            u=usage_entry(target)
            txt=(f"ℹ️ Пользователь {target}\n• 👑 Админ: {'да' if target in ADMINS else 'нет'}\n"
                 f"• 🌟 Премиум: {'да' if u.get('premium') else 'нет'}\n"
                 f"• Анализов (этот месяц): {u.get('count',0)} / лимит {CONFIG.get('FREE_LIMIT')}")
            return await q.message.reply_text(txt, reply_markup=admin_user_card_kb(target))

        if cmd=="act" and len(parts)>=4:
            action = parts[2]
            try: target = int(parts[3])
            except: return await q.message.reply_text("Некорректный user_id.")
            ensure_user(target)
            if action=="add_admin":
                ADMINS.add(target); persist_all()
                return await q.message.reply_text(f"✅ {target} назначен админом.", reply_markup=admin_user_card_kb(target))
            if action=="rem_admin":
                if target in ADMINS: ADMINS.remove(target); persist_all()
                return await q.message.reply_text(f"✅ {target} снят с админов.", reply_markup=admin_user_card_kb(target))
            if action=="grant_premium":
                u=usage_entry(target); u["premium"]=True; persist_all()
                return await q.message.reply_text(f"✅ Премиум выдан {target}.", reply_markup=admin_user_card_kb(target))
            if action=="revoke_premium":
                u=usage_entry(target); u["premium"]=False; persist_all()
                return await q.message.reply_text(f"✅ Премиум снят у {target}.", reply_markup=admin_user_card_kb(target))

        if cmd=="reload_refs":
            try:
                REF.reload_all()
                return await q.message.reply_text("✅ Справочники обновлены из Google Sheets")
            except Exception as e:
                return await q.message.reply_text(f"⚠️ Не удалось обновить справочники: {e}")

        if cmd=="stats":
            total=len(USERS); premium=sum(1 for u in USAGE.values() if u.get("premium"))
            month=datetime.utcnow().month
            total_analyses=sum(usage_entry(u)["count"] for u in USERS if usage_entry(u)["month"]==month)
            fb_up=FEEDBACK.get("up",0); fb_down=FEEDBACK.get("down",0)
            txt=(f"📊 Статистика:\n• Пользователей: {total}\n• Премиум: {premium}\n"
                 f"• Анализов (этот месяц): {total_analyses}\n• Фидбек 👍/👎: {fb_up}/{fb_down}\n"
                 f"• FREE_LIMIT: {CONFIG.get('FREE_LIMIT')} • PRICE: {CONFIG.get('PRICE_RUB')} ₽")
            return await q.message.reply_text(txt)

        if cmd=="bonus":
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Выдать премиум",callback_data="admin:pick_users")],
                                     [InlineKeyboardButton("⬅️ Назад",callback_data="admin")]])
            return await q.message.reply_text("🎁 Бонусы/Подарки", reply_markup=kb)

        if cmd=="settings":
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("🧮 Изменить лимит FREE",callback_data="admin:set_limit")],
                                     [InlineKeyboardButton("💵 Изменить цену",callback_data="admin:set_price")],
                                     [InlineKeyboardButton("⬅️ Назад",callback_data="admin")]])
            return await q.message.reply_text("⚙️ Настройки", reply_markup=kb)

        if cmd=="broadcast":
            ADMIN_STATE[uid]={"mode":"broadcast"}
            return await q.message.reply_text("Введи текст рассылки.")

        if cmd in ("set_limit","set_price"):
            ADMIN_STATE[uid]={"mode":cmd}
            if cmd=="set_limit":
                return await q.message.reply_text(f"FREE_LIMIT={CONFIG.get('FREE_LIMIT')}. Введи новое целое число.")
            else:
                return await q.message.reply_text(f"Цена={CONFIG.get('PRICE_RUB')} ₽. Введи новую цену (целое).")

def extract_user_id_from_message(update:Update)->int|None:
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id
    if update.message and update.message.forward_from:
        return update.message.forward_from.id
    if update.message and update.message.text:
        parts=update.message.text.strip().split()
        if parts and parts[0].isdigit(): return int(parts[0])
    return None

async def on_admin_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    admin_id=update.effective_user.id
    if not is_admin(admin_id): return
    st=ADMIN_STATE.get(admin_id)
    if not st: return
    mode=st.get("mode")

    if mode=="broadcast":
        text=update.message.text or ""; sent=failed=0
        for uid in list(USERS):
            try: await context.bot.send_message(uid, f"📣 Сообщение от администратора:\n\n{text}"); sent+=1
            except (Forbidden, Exception): failed+=1
        ADMIN_STATE.pop(admin_id, None); return await update.message.reply_text(f"Готово. Успешно: {sent}, ошибок: {failed}.")

    if mode=="set_limit":
        txt=(update.message.text or "").strip()
        if not txt.isdigit():
            return await update.message.reply_text("Введи целое число, например 5")
        CONFIG["FREE_LIMIT"]=int(txt); persist_all()
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"✅ FREE_LIMIT обновлён: {CONFIG['FREE_LIMIT']}")

    if mode=="set_price":
        txt=(update.message.text or "").strip()
        if not txt.isdigit():
            return await update.message.reply_text("Введи целую цену в ₽, например 299")
        CONFIG["PRICE_RUB"]=int(txt); persist_all()
        ADMIN_STATE.pop(admin_id, None)
        return await update.message.reply_text(f"✅ Цена обновлена: {CONFIG['PRICE_RUB']} ₽")

# ---------- ПРОСТЫЕ АДМИН-КОМАНДЫ ----------
async def cmd_whoami(update:Update, _):
    await update.message.reply_text(f"Твой user_id: <code>{update.effective_user.id}</code>", parse_mode="HTML")

async def cmd_make_admin_seed(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in seed_admins:
        return await update.message.reply_text("Недостаточно прав.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Использование: /make_admin <user_id>")
    target=int(context.args[0]); ADMINS.add(target); persist_all()
    await update.message.reply_text(f"✅ Пользователь {target} назначен админом.")

async def on_ping(update:Update,_): await update.message.reply_text("pong")

async def on_diag(update:Update,_):
    uid=update.effective_user.id
    if uid not in ADMINS: return await update.message.reply_text("Недостаточно прав.")
    total=len(USERS); premium=sum(1 for u in USAGE.values() if u.get("premium"))
    limit=int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT)); price=int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
    hist_path=os.path.abspath(HISTORY_DIR); hist_ok=True
    try:
        if HISTORY_ENABLED:
            p=os.path.join(HISTORY_DIR,".wtest"); open(p,"w").write("ok"); os.remove(p)
    except Exception: hist_ok=False
    txt=(f"<b>Диагностика</b>\n• Users: {total}\n• Premium: {premium}\n• FREE_LIMIT: {limit}\n• PRICE: {price} ₽\n"
         f"• History: {'on' if HISTORY_ENABLED else 'off'} ({'OK' if hist_ok else 'NO WRITE'})\n"
         f"• DATA_DIR: {os.path.abspath(DATA_DIR)}\n• Sheets: {'connected' if _sh else 'off'}")
    await update.message.reply_text(txt, parse_mode="HTML")

# ---------- HEALTHZ ----------
def start_flask_healthz(port:int):
    app=Flask(__name__)
    @app.get("/healthz")
    def healthz(): return "ok",200
    th=Thread(target=lambda: app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False))
    th.daemon=True; th.start(); log.info("Flask /healthz on %s", port)

# ---------- MAIN ----------
def main():
    app=Application.builder().token(BOT_TOKEN).build()

    profile_conv=ConversationHandler(
        entry_points=[CommandHandler("profile", profile_start_cmd),
                      CallbackQueryHandler(profile_start_cb, pattern="^profile$")],
        states={
            P_AGE:[MessageHandler(filters.TEXT & ~filters.COMMAND, profile_age)],
            P_SKIN:[MessageHandler(filters.TEXT & ~filters.COMMAND, profile_skin)],
            P_HAIR:[MessageHandler(filters.TEXT & ~filters.COMMAND, profile_hair)],
            P_GOALS:[MessageHandler(filters.TEXT & ~filters.COMMAND, profile_goals)],
        },
        fallbacks=[CommandHandler("cancel", profile_cancel)],
        name="profile_conv", persistent=False
    )
    app.add_handler(profile_conv)

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("ping", on_ping))
    app.add_handler(CommandHandler("diag", on_diag))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("make_admin", cmd_make_admin_seed))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_text))

    start_flask_healthz(PORT)
    sheets_init()
    try:
        REF.reload_all()
    except Exception as e:
        log.warning("RefData init failed: %s", e)

    app.run_polling()

if __name__=="__main__":
    try:
        log.info("Boot… ENV: BOT_TOKEN=%s, GEMINI_API_KEY=%s, SHEETS_ID=%s",
                 "SET" if os.getenv("BOT_TOKEN") else "MISSING",
                 "SET" if os.getenv("GEMINI_API_KEY") else "MISSING",
                 os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "-"))
        main()
    except Exception as e:
        import traceback, sys
        traceback.print_exc()
        log.error("Fatal on boot: %s", e)
        sys.exit(1)
