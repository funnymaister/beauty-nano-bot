# === main.py (Beauty Nano Bot) — правки админ-меню полностью рабочие ===
import os, io, re, time, json, base64, asyncio, logging, uuid
from datetime import datetime
from threading import Thread
from typing import Dict, Any, List

from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

# --- Sheets
import gspread
from google.oauth2.service_account import Credentials

# --- Flask Endpoints
from flask import Flask, request, jsonify

# --- Telegram
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, ConversationHandler, filters, PreCheckoutQueryHandler
)

# --- YooKassa
from yookassa import Configuration as YKConf, Payment as YKPayment

# --- RefData (messages/limits/… из Sheets)
from refdata import REF


# ========== ЛОГИ ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("beauty-nano-bot")
for noisy in ("httpx", "gspread", "google", "werkzeug"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ========== ENV / CONFIG ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not BOT_TOKEN: raise RuntimeError("Не задан BOT_TOKEN")
if not GEMINI_API_KEY: raise RuntimeError("Не задан GEMINI_API_KEY")

PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

# анализ
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "896"))

# лимиты/цены
DEFAULT_FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))
DEFAULT_PRICE_RUB = int(os.getenv("PRICE_RUB", "299"))

# history
HISTORY_ENABLED = os.getenv("HISTORY_ENABLED", "1") == "1"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))

# Google Sheets
SHEETS_ENABLED = os.getenv("SHEETS_ENABLED", "1") == "1"
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
SERVICE_JSON_B64 = os.getenv("GOOGLE_SHEETS_CREDS")

# Telegram Stars
STARS_PRICE_XTR = int(os.getenv("STARS_PRICE_XTR", "1200"))
STARS_PAY_TITLE = os.getenv("STARS_PAY_TITLE", "Премиум на 30 дней")
STARS_PAY_DESC  = os.getenv("STARS_PAY_DESC", "Безлимит анализов и приоритет")

# YooKassa
YK_SHOP_ID = os.getenv("YK_SHOP_ID")
YK_SECRET_KEY = os.getenv("YK_SECRET_KEY")
YK_RETURN_URL = os.getenv("YK_RETURN_URL", "https://example.com/yk/success")
if YK_SHOP_ID and YK_SECRET_KEY:
    YKConf.account_id = YK_SHOP_ID
    YKConf.secret_key = YK_SECRET_KEY

# файлы данных
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
    except Exception as e: log.warning("Can't save %s: %s", path, e)

# начальные структуры
def parse_admin_ids(val: str | None) -> set[int]:
    if not val: return set()
    ids=set()
    for p in val.replace(";",",").replace(" ",",").split(","):
        p=p.strip()
        if p.isdigit(): ids.add(int(p))
    return ids

seed_admins: set[int] = parse_admin_ids(os.getenv("ADMIN_IDS"))

ADMINS: set[int] = set(load_json(ADMINS_FILE, []))
if seed_admins: ADMINS |= seed_admins
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


# ========== GEMINI ==========
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")


# ========== GOOGLE SHEETS ==========
_gc=_sh=None
def _ensure_ws(title: str, headers: List[str]):
    try: return _sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws=_sh.add_worksheet(title=title, rows="200", cols=str(max(20, len(headers)+5)))
        ws.append_row(headers); return ws

def sheets_init():
    global _gc,_sh
    if not SHEETS_ENABLED: return
    if not SPREADSHEET_ID or not SERVICE_JSON_B64:
        log.warning("Sheets env missing"); return
    try:
        creds_info=json.loads(base64.b64decode(SERVICE_JSON_B64))
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
        credentials=Credentials.from_service_account_info(creds_info, scopes=scopes)
        _gc=gspread.authorize(credentials); _sh=_gc.open_by_key(SPREADSHEET_ID)
        _ensure_ws("users",    ["ts","user_id","username","is_admin","premium"])
        _ensure_ws("analyses", ["ts","user_id","username","mode","premium","free_used","text"])
        _ensure_ws("feedback", ["ts","user_id","value"])
        _ensure_ws("promos",   ["code","bonus_days","uses_left","expires_ts","note"])
        log.info("Sheets connected")
    except Exception as e:
        log.exception("Sheets init failed: %s", e)

def sheets_log_user(user_id:int, username:str|None):
    if not _sh: return
    try:
        _sh.worksheet("users").append_row(
            [int(time.time()), user_id, username or "", bool(user_id in ADMINS), bool(USAGE.get(user_id,{}).get("premium"))],
            value_input_option="USER_ENTERED")
    except Exception as e: log.warning("sheets_log_user failed: %s", e)

def sheets_log_analysis(user_id:int, username:str|None, mode:str, text:str):
    if not _sh: return
    try:
        u=USAGE.get(user_id, {})
        _sh.worksheet("analyses").append_row(
            [int(time.time()), user_id, username or "", mode, bool(u.get("premium")), int(u.get("count",0)), text[:10000]],
            value_input_option="USER_ENTERED")
    except Exception as e: log.warning("sheets_log_analysis failed: %s", e)

def sheets_log_feedback(user_id:int, value:str):
    if not _sh: return
    try:
        _sh.worksheet("feedback").append_row([int(time.time()), user_id, value], value_input_option="USER_ENTERED")
    except Exception as e: log.warning("sheets_log_feedback failed: %s", e)

def sheets_promo_get(code: str)->dict|None:
    if not _sh: return None
    try:
        ws=_sh.worksheet("promos"); rows=ws.get_all_records(numericise_ignore=["all"])
        code=code.strip().lower()
        for r in rows:
            if (r.get("code") or "").strip().lower()==code:
                return r
    except Exception as e: log.warning("promo get failed: %s", e)
    return None

def sheets_promo_decrement(code: str)->bool:
    if not _sh: return False
    try:
        ws=_sh.worksheet("promos"); data=ws.get_all_values()
        for i in range(1,len(data)):
            if data[i][0].strip().lower()==code.strip().lower():
                try:
                    uses_left=int(data[i][2]) if data[i][2].isdigit() else 0
                    if uses_left<=0: return False
                    data[i][2]=str(uses_left-1)
                    ws.update(f"A{i+1}:E{i+1}", [data[i]])
                    return True
                except: return False
    except Exception as e: log.warning("promo dec failed: %s", e)
    return False


# ========== ПОЛЬЗОВАТЕЛИ / ПРЕМИУМ ==========
def ensure_user(user_id:int):
    if user_id not in USERS: USERS.add(user_id); persist_all()

def usage_entry(user_id:int)->Dict[str,Any]:
    now=datetime.utcnow(); m=now.month
    u=USAGE.setdefault(user_id, {"count":0,"month":m,"premium":False})
    if int(u.get("premium_until", 0)) < int(time.time()):
        u["premium"] = False
    if u.get("month")!=m: u["count"]=0; u["month"]=m
    return u

def has_premium(user_id:int)->bool:
    u=usage_entry(user_id)
    if u.get("premium"): return True
    pu=int(u.get("premium_until",0))
    if pu and pu>int(time.time()):
        u["premium"]=True; return True
    return False

def grant_premium(user_id:int, days:int=30):
    u=usage_entry(user_id)
    base=max(int(time.time()), int(u.get("premium_until",0)))
    till=base+days*24*3600
    u["premium"]=True; u["premium_until"]=till
    persist_all(); return till

def extend_premium_days(user_id:int, days:int=30)->int:
    return grant_premium(user_id, days)

def disable_yk_autorenew(user_id:int)->bool:
    u=usage_entry(user_id)
    if "yk_payment_method_id" in u:
        u.pop("yk_payment_method_id", None); persist_all(); return True
    return False

def check_usage(user_id:int)->bool:
    u=usage_entry(user_id)
    if has_premium(user_id): return True
    limit=int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    if u["count"]<limit:
        u["count"]+=1; persist_all(); return True
    return False

def get_usage_text(user_id:int)->str:
    u=usage_entry(user_id)
    if has_premium(user_id):
        exp=datetime.fromtimestamp(int(u.get("premium_until", time.time()))).strftime("%d.%m.%Y")
        flags=[]
        if u.get("yk_payment_method_id"): flags.append("💳 YK авто")
        if u.get("stars_charge_id") and not u.get("stars_auto_canceled"): flags.append("⭐️ авто")
        flag_txt=(" ("+", ".join(flags)+")") if flags else ""
        return f"🌟 Премиум активен до {exp}{flag_txt}."
    limit=int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    left=max(0, limit-u["count"])
    return f"Осталось бесплатных анализов: {left} из {limit}."


# ========== СТИЛЬ / ТЕКСТ ==========
SAFE_CHUNK=3500
def html_escape(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _emoji_bullets(text: str) -> str:
    colors=["🟢","🟡","🔵","🟣","🟠"]; i=0; out=[]
    for line in text.splitlines():
        if re.match(r"^\s*(?:[•\-\*\u2022]|[0-9]+\.)\s+", line):
            bullet=colors[i%len(colors)]; i+=1
            line=re.sub(r"^\s*(?:[•\-\*\u2022]|[0-9]+\.)\s+", bullet+" ", line)
        line=re.sub(r"\b(утро|утренний)\b","☀️ утро", line, flags=re.I)
        line=re.sub(r"\b(день|днём|дневной)\b","🌤️ день", line, flags=re.I)
        line=re.sub(r"\b(вечер|вечерний)\b","🌙 вечер", line, flags=re.I)
        out.append(line)
    return "\n".join(out)

def _themed_headings(text: str) -> str:
    themed=[]
    for ln in text.splitlines():
        m=re.match(r"^\s*(утро|день|вечер|ноч[ььи]|ночной|sos|советы|рекомендац(ии|ия))\b[:\-–]?\s*(.*)$", ln, flags=re.I)
        if m:
            key=m.group(1).lower(); rest=m.group(3); emo="✨"
            if key.startswith("утро"): emo="☀️"
            elif key.startswith("день"): emo="🌤️"
            elif key.startswith("вечер"): emo="🌙"
            elif key.startswith("ноч"): emo="🌘"
            elif key=="sos": emo="🚑"
            elif key.startswith("советы") or key.startswith("рекомендац"): emo="🎯"
            title=key.capitalize()
            ln=f"<b>{emo} {html_escape(title)}</b>"
            if rest: ln+=f"\n{html_escape(rest)}"
            themed.append(ln)
        else:
            themed.append(html_escape(ln))
    return "\n".join(themed)

def _split_chunks(s: str, limit:int=SAFE_CHUNK)->list[str]:
    s=s.strip(); parts=[]
    while len(s)>limit:
        cut=s.rfind("\n\n",0,limit)
        if cut==-1: cut=s.rfind("\n",0,limit)
        if cut==-1: cut=limit
        parts.append(s[:cut].strip()); s=s[cut:].strip()
    if s: parts.append(s)
    return parts

async def send_html_long(chat, html_text:str, keyboard=None):
    chunks=_split_chunks(html_text, SAFE_CHUNK)
    if not chunks: return
    for part in chunks[:-1]:
        try: await chat.send_message(part, parse_mode="HTML")
        except BadRequest: await chat.send_message(re.sub(r"<[^>]+>","",part))
    last=chunks[-1]
    try: await chat.send_message(last, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest: await chat.send_message(re.sub(r"<[^>]+>","",last), reply_markup=keyboard)

# ---------- Режимы ----------
MODES = {"face": "Лицо", "hair": "Волосы", "both": "Лицо + Волосы"}

def get_mode(user_data: dict) -> str:
    return user_data.get("mode", "both")

def set_mode(user_data: dict, mode: str) -> None:
    if mode in MODES:
        user_data["mode"] = mode

def mode_keyboard(active: str) -> InlineKeyboardMarkup:
    def label(key):
        name = MODES.get(key, key)
        return f"✅ {name}" if key == active else name
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("face"), callback_data="mode:face"),
         InlineKeyboardButton(label("hair"), callback_data="mode:hair")],
        [InlineKeyboardButton(label("both"), callback_data="mode:both")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
    ])


# ========== History (локально + Sheets) ==========
HISTORY_ENABLED = HISTORY_ENABLED
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

def sheets_fetch_history(user_id:int, limit:int=20)->List[Dict[str,Any]]:
    if not _sh: return []
    try:
        ws=_sh.worksheet("analyses"); rows=ws.get_all_records(numericise_ignore=["all"])
        out=[]
        for r in rows:
            try:
                if int(str(r.get("user_id","-1")).strip())!=int(user_id): continue
                ts_raw=str(r.get("ts","")).strip()
                ts=int(ts_raw) if ts_raw.isdigit() else int(time.time())
                mode=(str(r.get("mode","both")) or "both").strip().lower()
                text=(r.get("text") or "").strip()
                out.append({"ts":ts,"mode":mode,"img":None,"txt_inline":text})
            except Exception: continue
        out.sort(key=lambda x:x["ts"], reverse=True)
        return out[:limit]
    except Exception as e:
        log.warning("sheets_fetch_history failed: %s", e)
        return []

def list_history(uid:int)->List[Dict[str,Any]]:
    local=HISTORY.get(str(uid),[])
    remote=sheets_fetch_history(uid, limit=20) if _sh else []
    norm=[]
    for e in local:
        norm.append({"ts":int(e["ts"]), "mode":e.get("mode","both"),
                     "img":e.get("img"), "txt":e.get("txt"), "txt_inline":None})
    for e in remote:
        norm.append({"ts":int(e["ts"]), "mode":e.get("mode","both"),
                     "img":None, "txt":None, "txt_inline":e.get("txt_inline","")})
    uniq={}
    for e in norm:
        uniq.setdefault(e["ts"], e)
    items=sorted(uniq.values(), key=lambda x:x["ts"], reverse=True)
    return items[:HISTORY_LIMIT]

def history_keyboard(uid:int)->InlineKeyboardMarkup:
    entries=list_history(uid)
    if not entries:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад",callback_data="home")]])
    rows=[]
    for e in entries[:10]:
        dt=datetime.fromtimestamp(e["ts"]).strftime("%d.%m %H:%M")
        mode={"face":"Лицо","hair":"Волосы","both":"Лицо + Волосы"}.get(e.get("mode","both"),"")
        rows.append([InlineKeyboardButton(f"📸 {dt} • {mode}", callback_data=f"hist:{e['ts']}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)


# ========== Кнопки ==========
def action_keyboard(for_user_id: int, user_data: dict | None = None) -> InlineKeyboardMarkup:
    premium = has_premium(for_user_id)
    rows = [
        [InlineKeyboardButton("🔄 Новый анализ", callback_data="home")],
        [InlineKeyboardButton("⚙️ Режим", callback_data="mode_menu"),
         InlineKeyboardButton("🧑‍💼 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🗂 История", callback_data="history")],
        [InlineKeyboardButton("👍 Полезно", callback_data="fb:up"),
         InlineKeyboardButton("👎 Не очень", callback_data="fb:down")],
        [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits"),
         InlineKeyboardButton("💳 Мои платежи", callback_data="payments_me")],
    ]
    if not premium:
        rows.append([InlineKeyboardButton("🌟 Премиум", callback_data="premium")])
    else:
        rows.append([InlineKeyboardButton("💳 Управление премиумом", callback_data="premium")])
    if for_user_id in ADMINS:
        rows.append([InlineKeyboardButton("🛠 Администратор", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

# ---------- Форматирование дат ----------
def human_dt(ts: int | float | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


# ---------- Админ-меню (клавиатуры) ----------
def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin:pick_users"),
         InlineKeyboardButton("📊 Статистика",  callback_data="admin:stats")],
        [InlineKeyboardButton("💳 Подписки",    callback_data="admin:subs"),
         InlineKeyboardButton("📣 Рассылка",    callback_data="admin:broadcast")],
        [InlineKeyboardButton("🎁 Бонусы",      callback_data="admin:bonus"),
         InlineKeyboardButton("⚙️ Настройки",   callback_data="admin:settings")],
        [InlineKeyboardButton("🔄 Обновить справочники", callback_data="admin:reload_refs")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
    ])

def admin_subs_list_kb() -> InlineKeyboardMarkup:
    now = int(time.time()); candidates = []
    for uid, u in USAGE.items():
        if int(u.get("premium_until", 0)) > now or u.get("yk_payment_method_id") or u.get("stars_charge_id"):
            candidates.append(int(uid))
    candidates = sorted(candidates, key=lambda i: int(USAGE.get(i, {}).get("premium_until", 0)), reverse=True)[:12]
    rows = []
    for i in candidates:
        u = usage_entry(i); exp = human_dt(u.get("premium_until"))
        star = "⭐️" if u.get("stars_charge_id") else ""
        yk   = "💳" if u.get("yk_payment_method_id") else ""
        rows.append([InlineKeyboardButton(f"{i} • до {exp} {star}{yk}", callback_data=f"admin:subs_user:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def admin_subs_user_kb(target_id: int) -> InlineKeyboardMarkup:
    u = usage_entry(target_id)
    has_stars = bool(u.get("stars_charge_id"))
    stars_canceled = bool(u.get("stars_auto_canceled"))
    has_yk = bool(u.get("yk_payment_method_id"))
    rows = []
    if has_stars:
        if not stars_canceled:
            rows.append([InlineKeyboardButton("⛔️ Отключить авто Stars", callback_data=f"admin:subs_action:stars_cancel:{target_id}")])
        else:
            rows.append([InlineKeyboardButton("♻️ Включить авто Stars",  callback_data=f"admin:subs_action:stars_enable:{target_id}")])
    if has_yk:
        rows.append([InlineKeyboardButton("⛔️ Отключить авто YooKassa", callback_data=f"admin:subs_action:yk_disable:{target_id}")])
    rows.append([InlineKeyboardButton("➕ +30 дней", callback_data=f"admin:subs_action:add30:{target_id}"),
                 InlineKeyboardButton("❌ Снять премиум", callback_data=f"admin:subs_action:clear:{target_id}")])
    rows.append([InlineKeyboardButton("⬅️ К списку", callback_data="admin:subs_list")])
    return InlineKeyboardMarkup(rows)

# ---------- Админ: Пользователи (клавиатуры) ----------
def _user_short_row(u_id: int) -> str:
    u = USAGE.get(u_id, {})
    prem = int(u.get("premium_until", 0)) > int(time.time())
    adm  = (u_id in ADMINS)
    badges = []
    if prem: badges.append("🌟")
    if adm:  badges.append("⭐")
    tag = " ".join(badges)
    exp = human_dt(u.get("premium_until"))
    return f"{u_id} • до {exp} {tag}".strip()

def admin_users_list_kb(page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    ids = sorted(list(USERS))
    total = len(ids)
    start = max(0, page * per_page)
    end   = min(total, start + per_page)
    page_ids = ids[start:end]

    rows: list[list[InlineKeyboardButton]] = []
    if not page_ids:
        rows.append([InlineKeyboardButton("Пока пусто", callback_data="noop")])
    else:
        for uid in page_ids:
            rows.append([InlineKeyboardButton(_user_short_row(uid), callback_data=f"admin:user:{uid}")])

    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:users_page:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"admin:users_page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🏠 В админ-меню", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def admin_user_card_kb(target_id: int) -> InlineKeyboardMarkup:
    u = usage_entry(target_id)
    is_admin = (target_id in ADMINS)
    rows = [
        [InlineKeyboardButton("➕ Продлить +30 дн.", callback_data=f"admin:user_action:add30:{target_id}")],
        [InlineKeyboardButton("❌ Снять премиум",    callback_data=f"admin:user_action:clear:{target_id}")],
        [InlineKeyboardButton("🔄 Сбросить бесплатные", callback_data=f"admin:user_action:resetfree:{target_id}")]
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("⭐ Убрать админа", callback_data=f"admin:user_action:unadmin:{target_id}")])
    else:
        rows.append([InlineKeyboardButton("⭐ Назначить админом", callback_data=f"admin:user_action:admin:{target_id}")])
    rows.append([InlineKeyboardButton("⬅️ К списку", callback_data="admin:pick_users")])
    rows.append([InlineKeyboardButton("🏠 В админ-меню", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

# ---------- Админ: Статистика / Настройки / Бонусы ----------
def admin_stats_text() -> str:
    total_users = len(USERS)
    premium_active = sum(1 for u in USAGE.values() if int(u.get("premium_until",0)) > int(time.time()))
    yk_saved = sum(1 for u in USAGE.values() if u.get("yk_payment_method_id"))
    stars_saved = sum(1 for u in USAGE.values() if u.get("stars_charge_id"))
    up = int(FEEDBACK.get("up",0)); down = int(FEEDBACK.get("down",0))
    # analyses
    analyses = 0
    if _sh:
        try:
            analyses = len(_sh.worksheet("analyses").get_all_values()) - 1
            if analyses < 0: analyses = 0
        except Exception: pass
    else:
        analyses = sum(len(v) for v in HISTORY.values())
    return (
        "📊 <b>Статистика</b>\n"
        f"• Пользователей: {total_users}\n"
        f"• Премиум активных: {premium_active}\n"
        f"• Сохранённый метод YooKassa: {yk_saved}\n"
        f"• Stars подписок: {stars_saved}\n"
        f"• Анализов: {analyses}\n"
        f"• Отзывы: 👍 {up} / 👎 {down}"
    )

def admin_settings_kb() -> InlineKeyboardMarkup:
    L = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    P = int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"FREE_LIMIT: {L}", callback_data="noop")],
        [InlineKeyboardButton("−1", callback_data="admin:cfg:limit:-1"),
         InlineKeyboardButton("+1", callback_data="admin:cfg:limit:+1"),
         InlineKeyboardButton("+10", callback_data="admin:cfg:limit:+10")],
        [InlineKeyboardButton(f"PRICE_RUB: {P}", callback_data="noop")],
        [InlineKeyboardButton("−10", callback_data="admin:cfg:price:-10"),
         InlineKeyboardButton("+10", callback_data="admin:cfg:price:+10"),
         InlineKeyboardButton("+100", callback_data="admin:cfg:price:+100")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin")]
    ])

def admin_bonus_kb(uid:int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Себе +7 дней", callback_data=f"admin:bonus_self:7"),
         InlineKeyboardButton("🎁 Себе +30 дней", callback_data=f"admin:bonus_self:30")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin")]
    ])


def premium_menu_kb()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 YooKassa (RUB)", callback_data="pay:yookassa")],
        [InlineKeyboardButton("⭐️ Telegram Stars",  callback_data="pay:stars")],
        [InlineKeyboardButton("🎁 Триал 24ч",       callback_data="trial"),
         InlineKeyboardButton("🎟️ Промокод",        callback_data="promo")],
        [InlineKeyboardButton("⬅️ Назад",           callback_data="home")],
    ])

# ---------- Профиль (опросник) ----------
P_AGE, P_SKIN, P_HAIR, P_GOALS = range(4)
def get_profile(user_data: dict) -> dict: return user_data.setdefault("profile", {})
def profile_to_text(pr: dict) -> str:
    if not pr: return "Профиль пуст."
    parts=[]
    if pr.get("age"):  parts.append(f"Возраст: {pr['age']}")
    if pr.get("skin"): parts.append(f"Кожа: {pr['skin']}")
    if pr.get("hair"): parts.append(f"Волосы: {pr['hair']}")
    if pr.get("goals"):parts.append(f"Цели: {pr['goals']}")
    return "\n".join(parts)

async def profile_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сколько тебе лет? (5–100)")
    return P_AGE
async def profile_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); await q.message.reply_text("Сколько тебе лет? (5–100)"); return P_AGE
async def profile_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t=(update.message.text or "").strip()
    if not t.isdigit() or not (5 <= int(t) <= 100):
        return await update.message.reply_text("Введи возраст числом от 5 до 100.")
    get_profile(context.user_data)["age"]=int(t)
    await update.message.reply_text("Опиши тип/состояние кожи (например: комбинированная, чувствительная):")
    return P_SKIN
async def profile_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["skin"]=(update.message.text or "").strip()[:100]
    await update.message.reply_text("Опиши тип/состояние волос (например: тонкие, склонны к жирности):")
    return P_HAIR
async def profile_hair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["hair"]=(update.message.text or "").strip()[:120]
    await update.message.reply_text("Какие цели/предпочтения? (например: меньше блеска, объём, без сульфатов):")
    return P_GOALS
async def profile_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_profile(context.user_data)["goals"]=(update.message.text or "").strip()[:160]
    await update.message.reply_text("Профиль сохранён:\n\n"+profile_to_text(get_profile(context.user_data)))
    await update.message.reply_text("Готово! Можешь прислать фото для анализа 💄",
                                    reply_markup=action_keyboard(update.effective_user.id, context.user_data))
    return ConversationHandler.END
async def profile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменил. /profile — начать заново.")
    return ConversationHandler.END


# ========== YooKassa ==========
def yk_create_first_payment(user_id:int, amount_rub:int)->str:
    if not (YK_SHOP_ID and YK_SECRET_KEY):
        raise RuntimeError("YooKassa не настроена")
    idemp=str(uuid.uuid4())
    payment=YKPayment.create({
        "amount":{"value":f"{amount_rub:.2f}","currency":"RUB"},
        "capture": True,
        "confirmation":{"type":"redirect","return_url":YK_RETURN_URL},
        "save_payment_method": True,
        "description": f"Beauty Nano Premium 30d (uid {user_id})",
        "metadata":{"user_id":str(user_id),"purpose":"premium_monthly","first":"1"}
    }, idempotency_key=idemp)
    return payment.confirmation.confirmation_url

def yk_charge_saved(user_id:int, amount_rub:int, payment_method_id:str)->bool:
    try:
        idemp=str(uuid.uuid4())
        YKPayment.create({
            "amount":{"value":f"{amount_rub:.2f}","currency":"RUB"},
            "capture": True,
            "payment_method_id": payment_method_id,
            "description": f"AutoRenew Premium (uid {user_id})",
            "metadata":{"user_id":str(user_id),"purpose":"premium_monthly","renew":"1"}
        }, idempotency_key=idemp)
        return True
    except Exception as e:
        log.warning("yk_charge_saved fail: %s", e)
        return False


# ========== Telegram Stars ==========
async def send_stars_invoice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    prices=[LabeledPrice(label="Премиум 30 дней", amount=STARS_PRICE_XTR)]
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=STARS_PAY_TITLE,
        description=STARS_PAY_DESC,
        payload=f"stars_premium_{chat_id}_{int(time.time())}",
        currency="XTR",
        prices=prices,
        subscription_period=2592000  # 30 дней
    )

async def tg_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def tg_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp=update.message.successful_payment
    if not sp: return
    uid=update.effective_user.id
    if sp.currency=="XTR":
        try:
            ch_id=sp.telegram_payment_charge_id
            u=usage_entry(uid)
            u["stars_charge_id"]=ch_id
            u["stars_auto_canceled"]=False
            persist_all()
        except Exception: pass
        exp_ts=getattr(sp,"subscription_expiration_date", None)
        if isinstance(exp_ts,int) and exp_ts>0:
            uu=usage_entry(uid); uu["premium"]=True; uu["premium_until"]=exp_ts; persist_all()
        else:
            grant_premium(uid, 30)
        await update.message.reply_text("✅ Премиум оплачен через ⭐️ Stars. Спасибо!", reply_markup=action_keyboard(uid, context.user_data))


# ========== ПРОМОКОДЫ / ТРИАЛ ==========
USER_STATE: Dict[int, Dict[str,Any]] = {}
def apply_promo(user_id:int, code:str)->str:
    rec=sheets_promo_get(code) if _sh else None
    if rec:
        try:
            exp=int(rec.get("expires_ts") or "0"); uses=int(rec.get("uses_left") or "0"); days=int(rec.get("bonus_days") or "0")
            if exp and int(time.time())>exp: return "⏳ Срок действия промокода истёк."
            if uses<=0: return "❌ Промокод уже исчерпан."
            if days>0:
                grant_premium(user_id, days=days)
                if not sheets_promo_decrement(code): log.warning("promo decrement failed")
                return f"✅ Активирован {days} дн. Премиума!"
            return "ℹ️ Промокод валиден, но бонус не задан."
        except Exception as e:
            log.warning("promo parse: %s", e)
            return "⚠️ Не удалось применить промокод."
    if code.strip().lower()=="free1d":
        grant_premium(user_id, 1); return "✅ 1 день Премиума активирован."
    return "❌ Промокод не найден."


# ========== АНАЛИЗ ФОТО ==========
async def run_blocking(func,*a,**kw): return await asyncio.to_thread(func,*a,**kw)

PHOTO_TIPS_PATTERNS=[r"улучш(ить|ения?)\s+(качества|фото|изображения)", r"качество\s+(фото|изображения)",
    r"освещени[ея]", r"ракурс", r"(камера|объектив|смартфон|зеркалк)", r"сделай(те)?\s+фото", r"пересним(и|ите)",
    r"перефотографируй(те)?", r"фон.*(равномерн|однотонн)", r"резкост[ьи]", r"шум(ы)?\s+на\s+фото",
    r"неч[её]тк(о|ость)|размыто", r"увеличь(те)?\s+разрешение"]
_photo_tips_rx=re.compile("|".join(PHOTO_TIPS_PATTERNS), re.I|re.U)
def remove_photo_tips(text:str)->str:
    parts=re.split(r"\n{2,}",(text or "").strip()); kept=[p for p in parts if not _photo_tips_rx.search(p)]
    return ("\n\n".join(kept).strip()) or text

LAST_ANALYSIS_AT: Dict[int,float] = {}
MODES={"face":"Лицо","hair":"Волосы","both":"Лицо + Волосы"}
def get_mode(user_data:dict)->str: return user_data.get("mode","both")
def set_mode(user_data:dict, m:str):
    if m in MODES: user_data["mode"]=m

async def _process_image_bytes(chat, img_bytes:bytes, mode:str, user_data:dict, user_id:int, username:str|None):
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
        log.exception("PIL convert"); return await chat.send_message("Не удалось обработать фото. Попробуй другое.")

    b64=base64.b64encode(jpeg_bytes).decode("utf-8")
    payload=[
        ("Ты бьюти-ассистент. Проанализируй фото в контексте режима: "
         f"{mode}. Дай практичные рекомендации (утро/день/вечер). Никаких советов про качество фото/ракурс."),
        {"inline_data":{"mime_type":"image/jpeg","data":b64}}
    ]
    try:
        resp=await run_blocking(model.generate_content, payload)
        text=(getattr(resp,"text","") or "").strip() or "Ответ пустой."
        text=remove_photo_tips(text)

        def style_response(raw_text:str, mode:str)->str:
            txt=_emoji_bullets(raw_text.strip().replace("\r","\n"))
            txt=_themed_headings(txt)
            head=f"<b>💄 Beauty Nano — {MODES.get(mode,'Анализ')}</b>\n━━━━━━━━━━━━━━━━\n"
            tail="\n<i>Готово! Пришли новое фото или измени режим ниже.</i>"
            return head+txt+tail

        await send_html_long(chat, style_response(text, mode), keyboard=action_keyboard(user_id, user_data))

        asyncio.create_task(run_blocking(save_history, user_id, mode, jpeg_bytes, text))
        asyncio.create_task(run_blocking(sheets_log_analysis, user_id, username, mode, text))
        await chat.send_message(get_usage_text(user_id))
    except Exception as e:
        log.exception("Gemini error"); await chat.send_message(f"Ошибка анализа: {e}")

async def on_photo(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; ensure_user(uid)
    now=time.time()
    if now-LAST_ANALYSIS_AT.get(uid,0)<RATE_LIMIT_SECONDS:
        return await update.message.reply_text("Подожди пару секунд ⏳")
    LAST_ANALYSIS_AT[uid]=now
    file=await update.message.photo[-1].get_file()
    buf=io.BytesIO(); await file.download_to_memory(out=buf)
    await _process_image_bytes(update.effective_chat, buf.getvalue(), get_mode(context.user_data), context.user_data, uid, getattr(update.effective_user,"username",None))


# ========== CALLBACKS ==========
ADMIN_STATE: Dict[int, Dict[str,Any]] = {}

def payments_me_kb(uid:int)->InlineKeyboardMarkup:
    u=usage_entry(uid)
    rows=[]
    if u.get("stars_charge_id"):
        if not u.get("stars_auto_canceled"):
            rows.append([InlineKeyboardButton("⛔️ Отключить авто Stars", callback_data="me:stars_cancel")])
        else:
            rows.append([InlineKeyboardButton("♻️ Включить авто Stars", callback_data="me:stars_enable")])
    if u.get("yk_payment_method_id"):
        rows.append([InlineKeyboardButton("⛔️ Отключить авто YooKassa", callback_data="me:yk_disable")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)

async def on_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; data=(q.data or "").strip()
    uid=update.effective_user.id; ensure_user(uid)

    if data=="home":
        await q.answer()
        return await q.message.reply_text("Пришли фото — сделаю анализ 💄", reply_markup=action_keyboard(uid, context.user_data))

    # премиум/платежи
    if data=="premium":
        await q.answer()
        price=int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        txt=(f"🌟 <b>Премиум</b>\n"
             f"• Безлимит анализов на 30 дней\n"
             f"• Цена: {price} ₽  /  ⭐️ {STARS_PRICE_XTR}\n"
             f"Выбери способ оплаты/активации:")
        return await q.message.reply_text(txt, parse_mode="HTML", reply_markup=premium_menu_kb())

    if data=="pay:yookassa":
        await q.answer()
        try:
            url=yk_create_first_payment(uid, int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB)))
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Открыть YooKassa", url=url)],
                                     [InlineKeyboardButton("⬅️ Назад", callback_data="premium")]])
            return await q.message.reply_text("Открой ссылку и оплати. Премиум активируется автоматически.", reply_markup=kb)
        except Exception as e:
            log.exception("yk create error: %s", e)
            return await q.message.reply_text("⚠️ Платёж через ЮKassa сейчас недоступен.")

    if data=="pay:stars":
        await q.answer()
        upd=Update(update.update_id, message=q.message)
        return await send_stars_invoice(upd, context)

    if data=="trial":
        await q.answer()
        u=usage_entry(uid)
        if u.get("trial_used"): return await q.message.reply_text("⏳ Триал уже использован.")
        u["trial_used"]=True; persist_all()
        till=grant_premium(uid, 1)
        return await q.message.reply_text(f"✅ Триал активирован до {human_dt(till)}!", reply_markup=action_keyboard(uid, context.user_data))

    if data=="promo":
        await q.answer(); USER_STATE[uid]={"await":"promo"}
        return await q.message.reply_text("Введи промокод одним сообщением:")

    if data=="payments_me":
        await q.answer()
        u=usage_entry(uid)
        exp=human_dt(u.get("premium_until"))
        txt=(f"💳 <b>Мои платежи</b>\n"
             f"• Премиум: {'активен' if has_premium(uid) else 'не активен'} (до {exp})\n"
             f"• Stars авто: {('включено' if (u.get('stars_charge_id') and not u.get('stars_auto_canceled')) else 'отключено')}\n"
             f"• YooKassa авто: {('включено' if u.get('yk_payment_method_id') else 'отключено')}")
        return await q.message.reply_text(txt, parse_mode="HTML", reply_markup=payments_me_kb(uid))

    if data=="me:yk_disable":
        ok=disable_yk_autorenew(uid)
        await q.answer("Отключено" if ok else "Уже было выключено")
        return await on_callback(Update(update.update_id, callback_query=update.callback_query), context)

    if data in ("me:stars_cancel","me:stars_enable"):
        u=usage_entry(uid); ch=u.get("stars_charge_id")
        if not ch:
            await q.answer("Нет активной Stars-подписки", show_alert=True)
            return
        try:
            is_canceled=(data=="me:stars_cancel")
            await context.bot.edit_user_star_subscription(user_id=uid, telegram_payment_charge_id=ch, is_canceled=is_canceled)
            u["stars_auto_canceled"]=is_canceled; persist_all()
            await q.answer("Готово")
        except AttributeError:
            return await q.message.reply_text("⚠️ Обнови python-telegram-bot до 21.8+ для управления Stars.")
        except Exception as e:
            return await q.message.reply_text(f"⚠️ Не удалось изменить подписку Stars: {e}")
        return await on_callback(Update(update.update_id, callback_query=update.callback_query), context)

    # --- фидбек ---
    if data == "fb:up":
        FEEDBACK["up"] = FEEDBACK.get("up", 0) + 1
        persist_all()
        try: sheets_log_feedback(uid, "up")
        except Exception: pass
        await q.answer("Спасибо! 💜")
        return await q.message.reply_text(
            f"👍 {FEEDBACK.get('up',0)}  |  👎 {FEEDBACK.get('down',0)}",
            reply_markup=action_keyboard(uid, context.user_data)
        )

    if data == "fb:down":
        FEEDBACK["down"] = FEEDBACK.get("down", 0) + 1
        persist_all()
        try: sheets_log_feedback(uid, "down")
        except Exception: pass
        await q.answer("Принято 👌")
        return await q.message.reply_text(
            f"👍 {FEEDBACK.get('up',0)}  |  👎 {FEEDBACK.get('down',0)}",
            reply_markup=action_keyboard(uid, context.user_data)
        )

    # режим
    if data == "mode_menu":
        await q.answer()
        cur = get_mode(context.user_data)
        return await q.message.reply_text(
            f"Текущий режим: {MODES.get(cur, cur)}\nВыбери:",
            reply_markup=mode_keyboard(cur)
        )
    if data.startswith("mode:"):
        await q.answer("Режим обновлён")
        m = data.split(":", 1)[1]; set_mode(context.user_data, m)
        return await q.message.reply_text(
            f"Режим установлен: {MODES.get(m, m)}\nПришли фото.",
            reply_markup=action_keyboard(uid, context.user_data)
        )

    # лимиты
    if data == "limits":
        await q.answer()
        free_limit = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
        price_rub  = int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        txt = ("ℹ️ <b>Лимиты и цена</b>\n"
               f"• Бесплатно: {free_limit} анализов/день\n"
               f"• Премиум: безлимит на 30 дней\n"
               f"• Цена: {price_rub} ₽  /  ⭐️ {STARS_PRICE_XTR}")
        return await q.message.reply_text(txt, parse_mode="HTML")

    # история
    if data == "history":
        await q.answer()
        entries = list_history(uid)
        if not entries:
            return await q.message.reply_text(
                "История пуста. Пришли фото — и я сохраню результат 📒",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Домой", callback_data="home")]])
            )
        return await q.message.reply_text("Выбери запись из истории:", reply_markup=history_keyboard(uid))
    if data.startswith("hist:"):
        await q.answer()
        try: ts = int(data.split(":",1)[1])
        except Exception: return await q.message.reply_text("Некорректная запись истории.", reply_markup=history_keyboard(uid))
        entry = next((e for e in list_history(uid) if int(e["ts"]) == ts), None)
        if not entry: return await q.message.reply_text("Запись не найдена.", reply_markup=history_keyboard(uid))
        async def _read_file_text(path:str)->str:
            try:
                with open(path,"r",encoding="utf-8") as f: return f.read()
            except Exception: return ""
        dt=datetime.fromtimestamp(int(entry["ts"])).strftime("%d.%m.%Y %H:%M")
        mode_title={"face":"Лицо","hair":"Волосы","both":"Лицо + Волосы"}.get(entry.get("mode","both"),"Анализ")
        head=f"<b>💄 История — {mode_title}</b>\n<i>{dt}</i>\n━━━━━━━━━━━━━━━━\n"
        text=entry.get("txt_inline") or (await asyncio.to_thread(_read_file_text, entry.get("txt",""))) or "Текст отсутствует."
        styled=_themed_headings(_emoji_bullets(text))
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="history")],
                                 [InlineKeyboardButton("🏠 Домой", callback_data="home")]])
        if entry.get("img") and os.path.exists(entry["img"]):
            try:
                with open(entry["img"], "rb") as f: await q.message.chat.send_photo(photo=f, caption=f"📸 {dt}")
            except Exception as e: log.warning("send_photo failed: %s", e)
        await send_html_long(q.message.chat, head+styled, keyboard=kb)

    # === ADMIN ROOT ===
    if data == "admin":
        if uid not in ADMINS: return await q.answer("Нет прав", show_alert=True)
        await q.answer()
        return await q.message.reply_text("🛠 Админ-панель", reply_markup=admin_main_keyboard())

    # === ADMIN SUBROUTES ===
    if data.startswith("admin:"):
        if uid not in ADMINS: return await q.answer("Нет прав", show_alert=True)
        await q.answer()
        parts = data.split(":"); cmd = parts[1] if len(parts)>1 else ""

        # --- Пользователи ---
        if cmd == "pick_users":
            return await q.message.reply_text("👥 Пользователи", reply_markup=admin_users_list_kb(page=0))
        if cmd == "users_page" and len(parts) >= 3:
            try: page = int(parts[2])
            except Exception: page = 0
            return await q.message.reply_text("👥 Пользователи", reply_markup=admin_users_list_kb(page=page))
        if cmd == "user" and len(parts) >= 3 and parts[2].isdigit():
            target = int(parts[2]); u = usage_entry(target)
            txt = (f"👤 Пользователь {target}\n"
                   f"• Премиум до: {human_dt(u.get('premium_until'))}\n"
                   f"• Бесплатных использовано: {u.get('count',0)} / {CONFIG.get('FREE_LIMIT', DEFAULT_FREE_LIMIT)}\n"
                   f"• Админ: {'да' if target in ADMINS else 'нет'}")
            return await q.message.reply_text(txt, reply_markup=admin_user_card_kb(target))
        if cmd == "user_action" and len(parts) >= 4:
            action = parts[2]
            try: target = int(parts[3])
            except Exception: return await q.message.reply_text("Некорректный user_id.", reply_markup=admin_main_keyboard())
            u = usage_entry(target)
            if action == "add30":
                till = extend_premium_days(target, 30)
                return await q.message.reply_text(f"✅ Продлено до {human_dt(till)}", reply_markup=admin_user_card_kb(target))
            if action == "clear":
                u["premium"] = False; u["premium_until"] = 0; persist_all()
                return await q.message.reply_text("✅ Премиум снят.", reply_markup=admin_user_card_kb(target))
            if action == "resetfree":
                u["count"] = 0; persist_all()
                return await q.message.reply_text("✅ Бесплатные попытки сброшены.", reply_markup=admin_user_card_kb(target))
            if action == "admin":
                ADMINS.add(target); persist_all()
                return await q.message.reply_text("✅ Пользователь назначен админом.", reply_markup=admin_user_card_kb(target))
            if action == "unadmin":
                if target in ADMINS: ADMINS.remove(target); persist_all()
                return await q.message.reply_text("✅ Права админа сняты.", reply_markup=admin_user_card_kb(target))

        # --- Подписки ---
        if cmd == "subs":      return await q.message.reply_text("💳 Управление подписками", reply_markup=admin_subs_list_kb())
        if cmd == "subs_list": return await q.message.reply_text("💳 Активные подписки:",   reply_markup=admin_subs_list_kb())
        if cmd == "subs_user" and len(parts) >= 3 and parts[2].isdigit():
            target=int(parts[2]); u=usage_entry(target)
            txt=(f"👤 Пользователь {target}\n"
                 f"• Премиум до: {human_dt(u.get('premium_until'))}\n"
                 f"• Stars charge: {u.get('stars_charge_id','—')}\n"
                 f"• YK PM: {u.get('yk_payment_method_id','—')}\n"
                 f"• Stars авто: {'отключено' if u.get('stars_auto_canceled') else 'включено'}")
            return await q.message.reply_text(txt, reply_markup=admin_subs_user_kb(target))
        if cmd == "subs_action" and len(parts) >= 4:
            action=parts[2]; target=int(parts[3]); u=usage_entry(target)
            if action=="add30":
                till=extend_premium_days(target,30)
                return await q.message.reply_text(f"✅ Продлено до {human_dt(till)}", reply_markup=admin_subs_user_kb(target))
            if action=="clear":
                u["premium"]=False; u["premium_until"]=0; persist_all()
                return await q.message.reply_text("✅ Премиум снят.", reply_markup=admin_subs_user_kb(target))
            if action=="yk_disable":
                ok=disable_yk_autorenew(target)
                msg="✅ Автопродление YooKassa отключено." if ok else "ℹ️ Сохранённого метода YooKassa не было."
                return await q.message.reply_text(msg, reply_markup=admin_subs_user_kb(target))
            if action in ("stars_cancel","stars_enable"):
                ch=u.get("stars_charge_id")
                if not ch:
                    return await q.message.reply_text("ℹ️ Нет активной Stars-подписки.", reply_markup=admin_subs_user_kb(target))
                try:
                    is_canceled=(action=="stars_cancel")
                    await context.bot.edit_user_star_subscription(user_id=target, telegram_payment_charge_id=ch, is_canceled=is_canceled)
                    u["stars_auto_canceled"]=is_canceled; persist_all()
                    state="отключено" if is_canceled else "включено"
                    return await q.message.reply_text(f"✅ Автопродление Stars {state}.", reply_markup=admin_subs_user_kb(target))
                except AttributeError:
                    return await q.message.reply_text("⚠️ Обнови python-telegram-bot до 21.8+ для управления Stars.")
                except Exception as e:
                    return await q.message.reply_text(f"⚠️ Не удалось изменить Stars: {e}", reply_markup=admin_subs_user_kb(target))

        # --- Статистика ---
        if cmd == "stats":
            return await q.message.reply_text(admin_stats_text(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]))

        # --- Рассылка ---
        if cmd == "broadcast":
            ADMIN_STATE[uid] = {"await": "broadcast"}
            return await q.message.reply_text("📣 Пришли текст рассылки одним сообщением.\nОтправлю всем пользователям. /cancel — отмена.")

        # --- Бонусы ---
        if cmd == "bonus":
            return await q.message.reply_text("🎁 Быстрые бонусы", reply_markup=admin_bonus_kb(uid))

        if cmd == "bonus_self" and len(parts) >= 3 and parts[2].isdigit():
            days = int(parts[2])
            till = extend_premium_days(uid, days)
            return await q.message.reply_text(f"✅ Выдано себе +{days} дн. Премиума (до {human_dt(till)}).", reply_markup=admin_main_keyboard())

        # --- Настройки ---
        if cmd == "settings":
            return await q.message.reply_text("⚙️ Настройки", reply_markup=admin_settings_kb())

        if cmd == "cfg" and len(parts) >= 4:
            what = parts[2]; delta_raw = parts[3]
            try:
                delta = int(delta_raw)
            except:
                delta = 0
            if what == "limit":
                CONFIG["FREE_LIMIT"] = max(0, int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT)) + delta)
            if what == "price":
                CONFIG["PRICE_RUB"] = max(0, int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB)) + delta)
            persist_all()
            return await q.message.reply_text("⚙️ Настройки обновлены", reply_markup=admin_settings_kb())

        # --- Обновить справочники ---
        if cmd == "reload_refs":
            try:
                REF.reload_all()
                return await q.message.reply_text("✅ Справочники обновлены.", reply_markup=admin_main_keyboard())
            except Exception as e:
                return await q.message.reply_text(f"⚠️ Не удалось обновить: {e}", reply_markup=admin_main_keyboard())

    log.info("callback data=%s", data)


async def on_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    st=USER_STATE.get(uid)
    if st and st.get("await")=="promo":
        code=(update.message.text or "").strip()
        USER_STATE.pop(uid,None)
        msg=apply_promo(uid, code)
        return await update.message.reply_text(msg, reply_markup=action_keyboard(uid, context.user_data))

    # админская рассылка
    ast = ADMIN_STATE.get(uid)
    if uid in ADMINS and ast and ast.get("await") == "broadcast":
        ADMIN_STATE.pop(uid, None)
        text = (update.message.text or "").strip()
        sent = 0; fail = 0
        for to_id in list(USERS):
            try:
                await context.bot.send_message(to_id, text)
                sent += 1
                await asyncio.sleep(0.03)  # бережно
            except Forbidden:
                fail += 1
            except Exception:
                fail += 1
        return await update.message.reply_text(f"📣 Готово: отправлено {sent}, ошибок {fail}.", reply_markup=admin_main_keyboard())

    # здесь можно оставить другие админ-тексты…


# ========== HEALTHZ / WEBHOOKS ==========
def start_flask_endpoints(port:int):
    app=Flask(__name__)

    @app.get("/healthz")
    def healthz(): return "ok",200

    @app.get("/yk/success")
    def yk_success(): return "✅ Оплата принята. Вернись в Telegram — премиум активируется автоматически.",200

    @app.post("/yookassa/webhook")
    def yk_webhook():
        try: event=request.get_json(force=True, silent=False)
        except Exception: return jsonify({"error":"bad json"}),400
        try:
            e_type=event.get("event"); obj=event.get("object") or {}
            if e_type=="payment.succeeded":
                meta=obj.get("metadata") or {}; uid=int(meta.get("user_id","0")) if str(meta.get("user_id","0")).isdigit() else 0
                if uid:
                    pm=(obj.get("payment_method") or {}).get("id")
                    u=usage_entry(uid)
                    if pm: u["yk_payment_method_id"]=pm
                    till=grant_premium(uid, 30); persist_all()
                    log.info("YooKassa paid: uid=%s until=%s pm=%s", uid, till, pm)
            return jsonify({"ok":True}),200
        except Exception as e:
            log.exception("yk webhook error: %s", e)
            return jsonify({"ok":False}),200

    th=Thread(target=lambda: app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False))
    th.daemon=True; th.start(); log.info("Flask: /healthz, /yk/success, /yookassa/webhook on %s", port)


# ========== АВТОПРОДЛЕНИЕ (YK) ==========
async def autorenew_loop():
    while True:
        try:
            price=int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
            now=int(time.time())
            for uid,u in list(USAGE.items()):
                pm=u.get("yk_payment_method_id")
                until=int(u.get("premium_until",0))
                if pm and until and until-now<6*3600:
                    ok=yk_charge_saved(uid, price, pm)
                    log.info("AutoRenew try uid=%s ok=%s", uid, ok)
                    if ok:
                        u["premium"]=True
                        u["premium_until"]=until + 30*24*3600
                        persist_all()
        except Exception as e:
            log.warning("autorenew loop error: %s", e)
        await asyncio.sleep(3600)


# ========== START / MISC ==========
async def on_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; ensure_user(uid)
    sheets_log_user(uid, getattr(update.effective_user,"username",None))
    await update.message.reply_text("Привет! Пришли фото — сделаю анализ 💄", reply_markup=action_keyboard(uid, context.user_data))
    await update.message.reply_text(get_usage_text(uid))

async def on_ping(update:Update,_): await update.message.reply_text("pong")

def main():
    app=Application.builder().token(BOT_TOKEN).build()
    profile_conv = ConversationHandler(
        entry_points=[
            CommandHandler("profile", profile_start_cmd),
            CallbackQueryHandler(profile_start_cb, pattern=r"^profile$")
        ],
        states={
            P_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_age)],
            P_SKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_skin)],
            P_HAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_hair)],
            P_GOALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_goals)],
        },
        fallbacks=[CommandHandler("cancel", profile_cancel)],
        name="profile_conv",
        persistent=False,
    )
    app.add_handler(profile_conv)
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("ping", on_ping))

    # Платежи Stars
    app.add_handler(PreCheckoutQueryHandler(tg_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, tg_successful_payment))

    # Фото
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    # Кнопки
    app.add_handler(CallbackQueryHandler(on_callback))

    # Текст (промокод/рассылка)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    start_flask_endpoints(PORT)
    sheets_init()
    try: REF.reload_all()
    except Exception as e: log.warning("RefData init failed: %s", e)

    asyncio.get_event_loop().create_task(autorenew_loop())

    app.run_polling()

if __name__=="__main__":
    main()
