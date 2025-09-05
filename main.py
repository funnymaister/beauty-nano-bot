# === main.py (Beauty Nano Bot) — персонализация профилем + админ-меню ===
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

# --- RefData
try:
    from refdata import REF
except Exception:
    class _DummyRef:
        def reload_all(self): pass
    REF = _DummyRef()

# ========== ЛОГИ ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("beauty-nano-bot")
for noisy in ("httpx", "gspread", "google", "werkzeug", "yookassa"):
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

RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "896"))

DEFAULT_FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))
DEFAULT_PRICE_RUB  = int(os.getenv("PRICE_RUB",  "299"))

HISTORY_ENABLED = os.getenv("HISTORY_ENABLED", "1") == "1"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))

SHEETS_ENABLED = os.getenv("SHEETS_ENABLED", "1") == "1"
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
SERVICE_JSON_B64 = os.getenv("GOOGLE_SHEETS_CREDS")

STARS_PRICE_XTR = int(os.getenv("STARS_PRICE_XTR", "1200"))
STARS_PAY_TITLE = os.getenv("STARS_PAY_TITLE", "Премиум на 30 дней")
STARS_PAY_DESC  = os.getenv("STARS_PAY_DESC", "Безлимит анализов и приоритет")

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

# (остальной код, включая Sheets, Users, Premium, Style, Режимы, History, Admin keyboards и Профиль)
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

# ---------- Персонализация по профилю ----------
def _profile_context(user_data: dict) -> tuple[str, str]:
    pr = get_profile(user_data)
    parts = []
    if pr.get("age"):  parts.append(f"Возраст: {pr['age']}")
    if pr.get("skin"): parts.append(f"Кожа: {pr['skin']}")
    if pr.get("hair"): parts.append(f"Волосы: {pr['hair']}")
    if pr.get("goals"):parts.append(f"Цели: {pr['goals']}")
    human = "; ".join(parts)

    rules = []
    age  = pr.get("age")
    skin = (pr.get("skin") or "").lower()
    hair = (pr.get("hair") or "").lower()
    goals= (pr.get("goals") or "").lower()

    try:
        if age and int(age) < 18:
            rules.append("До 18 лет: избегай ретиноидов и сильных кислот (>5%); только мягкий уход.")
    except Exception:
        pass

    g = f"{skin} {hair} {goals}"
    if any(k in g for k in ["беремен", "pregnan", "гв", "лактац"]):
        rules.append("Беременность/лактация: без ретиноидов и BHA >1%, без агрессивных отдушек/эфирных масел; приоритет — ниацинамид, пантенол, церамиды, SPF.")
    if any(k in g for k in ["розаце", "купероз", "rosacea"]):
        rules.append("Розацеа/купероз: избегай AHA/BHA высокой концентрации и ретиноидов; только деликатные формулы, без спиртов и отдушек; ежедневный SPF.")
    if any(k in g for k in ["себор", "sd", "seborr"]):
        rules.append("Себорейный дерматит: мягкое очищение, противовоспалительные компоненты (цинк PCA, пироктон оламин); избегай агрессивных ПАВ/скрабов.")

    if "чувств" in skin:
        rules.append("Кожа чувствительная: без отдушек и спиртов; избегай сильных кислот; пантенол/церамиды/алоэ.")
    if "жир" in skin or "акне" in skin:
        rules.append("Кожа жирная/склонная к акне: лёгкие формулы; при необходимости BHA 1–2%; SPF без масел.")
    if "сух" in skin:
        rules.append("Кожа сухая: мягкое очищение, липидное восстановление, увлажнение вечером.")
    if "пигмент" in skin:
        rules.append("Пигментация: дневной SPF обязателен; мягкие осветляющие (ниацинамид, арбутин).")

    if "кудр" in hair:
        rules.append("Кудрявые волосы: без сульфатов; кондиционирование; диффузор на низком нагреве.")
    if "крашен" in hair or "осветл" in hair:
        rules.append("Окрашенные/повреждённые: бережные шампуни, маски с протеинами/липидами, термозащита.")

    if goals:
        rules.append(f"Приоритизируй цели пользователя: {goals}.")

    base = (
        "Учитывай персональные правила ниже. Если правило конфликтует с общим советом — выбирай мягкий и безопасный вариант. "
        "Дай практичные списки для ☀️ утро / 🌤️ день / 🌙 вечер. Не обсуждай качество фото."
    )
    rules_text = ("Правила персонализации:\n- " + "\n- ".join(rules)) if rules else \
                 "Правила персонализации: нет особых ограничений."
    return human, base + "\n" + rules_text

# ========== АНАЛИЗ ФОТО ==========
LAST_ANALYSIS_AT: Dict[int,float] = {}

# ===== REPLACE WHOLE FUNCTION _process_image_bytes WITH THIS ONE =====
LAST_ANALYSIS_AT: Dict[int, float] = {}

async def _process_image_bytes(
    chat,
    img_bytes: bytes,
    mode: str,
    user_data: dict,
    user_id: int,
    username: str | None,
):
    """Подготовка фото, формирование персонализированного промпта и вызов Gemini."""
    # лимит бесплатных попыток
    if not check_usage(user_id):
        return await chat.send_message(
            "🚫 Лимит исчерпан. Оформи 🌟 Премиум.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🌟 Купить Премиум", callback_data="premium")],
                    [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")],
                ]
            ),
        )

    # мягкая подсказка заполнить профиль, если пустой
    pr = get_profile(user_data)
    if not any(pr.get(k) for k in ("age", "skin", "hair", "goals")):
        try:
            await chat.send_message(
                "Хочешь более точные рекомендации? Заполни короткий профиль 🧑‍💼",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🧑‍💼 Заполнить профиль", callback_data="profile")]]
                ),
            )
        except Exception:
            pass

    # подготовка изображения
    try:
        def _prep(b: bytes) -> bytes:
            im = Image.open(io.BytesIO(b)).convert("RGB")
            im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue()

        jpeg_bytes = await asyncio.to_thread(_prep, img_bytes)
    except Exception:
        log.exception("PIL convert")
        return await chat.send_message("Не удалось обработать фото. Попробуй другое.")

    # персональные правила из профиля
    human_profile, rule_block = _profile_context(user_data)

    # сбор промпта + вызов модели
    try:
        b64 = base64.b64encode(jpeg_bytes).decode("utf-8")

        system_prompt = (
            "Ты бьюти-ассистент. Проанализируй фото в контексте режима: "
            f"{mode}. Учитывай анкету пользователя и правила ниже.\n\n"
            f"{rule_block}"
        )

        payload = [
            system_prompt,
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
        ]

        resp = await asyncio.to_thread(model.generate_content, payload)
        text = (getattr(resp, "text", "") or "").strip() or "Ответ пустой."
        try:
            # если у тебя есть фильтр качества фото — раскомментируй:
            # text = remove_photo_tips(text)
            pass
        except Exception:
            pass

        # оформление ответа
        def style_response(raw_text: str, mode: str) -> str:
            txt = _emoji_bullets(raw_text.strip().replace("\r", "\n"))
            txt = _themed_headings(txt)
            head = f"<b>💄 Beauty Nano — {MODES.get(mode, 'Анализ')}</b>\n"
            badge = f"<i>ℹ️ Профиль: {html_escape(human_profile)}</i>\n" if human_profile else ""
            sep = "━━━━━━━━━━━━━━━━\n"
            tail = "\n<i>Готово! Пришли новое фото или измени режим ниже.</i>"
            return head + badge + sep + txt + tail

        await send_html_long(chat, style_response(text, mode), keyboard=action_keyboard(user_id, user_data))

        # логирование и история — не блокируем основной поток
        asyncio.create_task(asyncio.to_thread(save_history, user_id, mode, jpeg_bytes, text))
        asyncio.create_task(asyncio.to_thread(sheets_log_analysis, user_id, username, mode, text))

        await chat.send_message(get_usage_text(user_id))
    except Exception as e:
        log.exception("Gemini error")
        await chat.send_message(f"Ошибка анализа: {e}")
# ===== END OF REPLACEMENT =====


async def on_photo(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; ensure_user(uid)
    now=time.time()
    if now-LAST_ANALYSIS_AT.get(uid,0)<RATE_LIMIT_SECONDS:
        return await update.message.reply_text("Подожди пару секунд ⏳")
    LAST_ANALYSIS_AT[uid]=now
    file=await update.message.photo[-1].get_file()
    buf=io.BytesIO(); await file.download_to_memory(out=buf)
    await _process_image_bytes(
        update.effective_chat, buf.getvalue(),
        get_mode(context.user_data), context.user_data, uid,
        getattr(update.effective_user,"username",None)
    )

# ---------- Стиль/текст (хелперы) ----------
SAFE_CHUNK = 3500
def html_escape(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _emoji_bullets(text: str) -> str:
    colors=["🟢","🟡","🔵","🟣","🟠"]; i=0; out=[]
    for line in (text or "").splitlines():
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
    for ln in (text or "").splitlines():
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

from contextlib import suppress
from telegram.error import BadRequest

async def safe_answer(q):
    """Мягко отвечает на callback_query и игнорирует протухший/невалидный id."""
    if not q:
        return
    with suppress(BadRequest, TimeoutError, Exception):
        # cache_time не обязателен, но иногда помогает телеграму не спамить
        await q.answer(cache_time=1)


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

# ---------- История (локально + Sheets) ----------
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
        def _ensure_ws(title: str, headers: List[str]):
            try: return _sh.worksheet(title)
            except gspread.WorksheetNotFound:
                ws=_sh.add_worksheet(title=title, rows="200", cols=str(max(20, len(headers)+5)))
                ws.append_row(headers); return ws
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

# ---------- Пользователи/лимиты/цены ----------
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
        return f"🌟 Премиум активен до {exp}."
    limit=int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
    left=max(0, limit-u["count"])
    return f"Осталось бесплатных анализов: {left} из {limit}."

def ensure_user(user_id:int):
    if user_id not in USERS: USERS.add(user_id); persist_all()

# ---------- Кнопки главные ----------
def action_keyboard(for_user_id: int, user_data: dict | None = None) -> InlineKeyboardMarkup:
    premium = has_premium(for_user_id)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🔄 Новый анализ", callback_data="home")],
        [InlineKeyboardButton("⚙️ Режим", callback_data="mode_menu"),
         InlineKeyboardButton("🧑‍💼 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🗂 История", callback_data="history")],
        [InlineKeyboardButton("👍 Полезно", callback_data="fb:up"),
         InlineKeyboardButton("👎 Не очень", callback_data="fb:down")],
        [InlineKeyboardButton("ℹ️ Лимиты", callback_data="limits")],
    ]
    if premium:
        rows.append([InlineKeyboardButton("💳 Мои платежи", callback_data="payments_me")])
    else:
        rows.append([InlineKeyboardButton("🌟 Премиум", callback_data="premium")])
    if for_user_id in ADMINS:
        rows.append([InlineKeyboardButton("🛠 Администратор", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def premium_menu_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # YooKassa показываем только если настроена
    if os.getenv("YK_SHOP_ID") and os.getenv("YK_SECRET_KEY"):
        rows.append([InlineKeyboardButton("💳 YooKassa (RUB)", callback_data="pay:yookassa")])
    # Stars — всегда
    rows.append([InlineKeyboardButton("⭐️ Telegram Stars", callback_data="pay:stars")])
    # Триал и промокод
    rows.append([
        InlineKeyboardButton("🎁 Триал 24ч", callback_data="trial"),
        InlineKeyboardButton("🎟️ Промокод", callback_data="promo")
    ])
    # Назад
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)


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

def _user_short_row(u_id: int) -> str:
    u = USAGE.get(u_id, {})
    prem = int(u.get("premium_until", 0)) > int(time.time())
    adm  = (u_id in ADMINS)
    badges = []
    if prem: badges.append("🌟")
    if adm:  badges.append("⭐")
    tag = " ".join(badges)
    exp = datetime.fromtimestamp(u.get("premium_until",0)).strftime("%d.%m.%Y %H:%M") if u.get("premium_until") else "—"
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

def admin_subs_list_kb() -> InlineKeyboardMarkup:
    now = int(time.time()); candidates = []
    for uid, u in USAGE.items():
        if int(u.get("premium_until", 0)) > now:
            candidates.append(int(uid))
    candidates = sorted(candidates, key=lambda i: int(USAGE.get(i, {}).get("premium_until", 0)), reverse=True)[:12]
    rows = []
    for i in candidates:
        u = usage_entry(i); exp = datetime.fromtimestamp(u.get("premium_until",0)).strftime("%d.%m.%Y %H:%M") if u.get("premium_until") else "—"
        rows.append([InlineKeyboardButton(f"{i} • до {exp}", callback_data=f"admin:subs_user:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def admin_subs_user_kb(target_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ +30 дней", callback_data=f"admin:subs_action:add30:{target_id}"),
         InlineKeyboardButton("❌ Снять премиум", callback_data=f"admin:subs_action:clear:{target_id}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="admin:subs_list")]
    ]
    return InlineKeyboardMarkup(rows)

# ---------- CallbackHandler ----------
ADMIN_STATE: Dict[int, Dict[str,Any]] = {}
USER_STATE:  Dict[int, Dict[str,Any]] = {}

def payments_me_kb(uid:int)->InlineKeyboardMarkup:
    u=usage_entry(uid)
    rows=[[InlineKeyboardButton("⬅️ Назад", callback_data="home")]]
    return InlineKeyboardMarkup(rows)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = (q.data or "").strip()
    uid = update.effective_user.id
    ensure_user(uid)

    # ВАЖНО: отвечаем сразу, ДО любых долгих операций
    await safe_answer(q)

    # дальше твоя логика:
    if data == "home":
        return await q.message.reply_text("Пришли фото — сделаю анализ 💄",
                                          reply_markup=action_keyboard(uid, context.user_data))
    ...


    # профиль (из кнопки)
    if data == "profile":
        await q.answer()
        return await profile_start_cb(update, context)

    # премиум/лимиты
    if data == "premium":
        price = int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        txt = (
            "🌟 <b>Премиум</b>\n"
            "• Безлимит анализов на 30 дней\n"
            f"• Цена: {price} ₽  /  ⭐️ {STARS_PRICE_XTR}\n"
            "Выбери способ оплаты/активации:"
        )
        return await q.message.reply_text(txt, parse_mode="HTML", reply_markup=premium_menu_kb())

    if data=="limits":
        await q.answer()
        free_limit = int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT))
        price_rub  = int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB))
        txt = ("ℹ️ <b>Лимиты и цена</b>\n"
               f"• Бесплатно: {free_limit} анализов/день\n"
               f"• Премиум: безлимит на 30 дней\n"
               f"• Цена: {price_rub} ₽  /  ⭐️ {STARS_PRICE_XTR}")
        return await q.message.reply_text(txt, parse_mode="HTML")

    # история
    if data=="history":
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

    # фидбек
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

    # админ
    if data == "admin":
        if uid not in ADMINS: return await q.answer("Нет прав", show_alert=True)
        await q.answer()
        return await q.message.reply_text("🛠 Админ-панель", reply_markup=admin_main_keyboard())

    if data.startswith("admin:"):
        if uid not in ADMINS: return await q.answer("Нет прав", show_alert=True)
        await q.answer()
        parts = data.split(":"); cmd = parts[1] if len(parts)>1 else ""

        if cmd == "pick_users":
            return await q.message.reply_text("👥 Пользователи", reply_markup=admin_users_list_kb(page=0))
        if cmd == "users_page" and len(parts) >= 3:
            try: page = int(parts[2])
            except Exception: page = 0
            return await q.message.reply_text("👥 Пользователи", reply_markup=admin_users_list_kb(page=page))
        if cmd == "user" and len(parts) >= 3 and parts[2].isdigit():
            target = int(parts[2]); u = usage_entry(target)
            exp = datetime.fromtimestamp(u.get('premium_until',0)).strftime("%d.%m.%Y %H:%M") if u.get("premium_until") else "—"
            txt = (f"👤 Пользователь {target}\n"
                   f"• Премиум до: {exp}\n"
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
                return await q.message.reply_text(f"✅ Продлено до {datetime.fromtimestamp(till):%d.%m.%Y %H:%M}", reply_markup=admin_user_card_kb(target))
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

        if cmd == "stats":
            total_users = len(USERS)
            premium_active = sum(1 for u in USAGE.values() if int(u.get("premium_until",0)) > int(time.time()))
            up = int(FEEDBACK.get("up",0)); down = int(FEEDBACK.get("down",0))
            analyses = 0
            if _sh:
                try:
                    analyses = len(_sh.worksheet("analyses").get_all_values()) - 1
                    if analyses < 0: analyses = 0
                except Exception: pass
            else:
                analyses = sum(len(v) for v in HISTORY.values())
            txt = ("📊 <b>Статистика</b>\n"
                   f"• Пользователей: {total_users}\n"
                   f"• Премиум активных: {premium_active}\n"
                   f"• Анализов: {analyses}\n"
                   f"• Отзывы: 👍 {up} / 👎 {down}")
            return await q.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]))

        if cmd == "broadcast":
            ADMIN_STATE[uid] = {"await": "broadcast"}
            return await q.message.reply_text("📣 Пришли текст рассылки одним сообщением.\nОтправлю всем пользователям. /cancel — отмена.")

        if cmd == "bonus":
            till = extend_premium_days(uid, 7)
            return await q.message.reply_text(f"🎁 Себе выдано +7 дн. (до {datetime.fromtimestamp(till):%d.%m.%Y %H:%M})", reply_markup=admin_main_keyboard())

        if cmd == "settings":
            return await q.message.reply_text("⚙️ Настройки", reply_markup=admin_settings_kb())

        if cmd == "cfg" and len(parts) >= 4:
            what = parts[2]; delta_raw = parts[3]
            try: delta = int(delta_raw)
            except: delta = 0
            if what == "limit":
                CONFIG["FREE_LIMIT"] = max(0, int(CONFIG.get("FREE_LIMIT", DEFAULT_FREE_LIMIT)) + delta)
            if what == "price":
                CONFIG["PRICE_RUB"] = max(0, int(CONFIG.get("PRICE_RUB", DEFAULT_PRICE_RUB)) + delta)
            persist_all()
            return await q.message.reply_text("⚙️ Настройки обновлены", reply_markup=admin_settings_kb())

        if cmd == "subs":
            return await q.message.reply_text("💳 Управление подписками", reply_markup=admin_subs_list_kb())
        if cmd == "subs_list":
            return await q.message.reply_text("💳 Активные подписки:",   reply_markup=admin_subs_list_kb())
        if cmd == "subs_user" and len(parts) >= 3 and parts[2].isdigit():
            target=int(parts[2]); u=usage_entry(target)
            exp=datetime.fromtimestamp(u.get('premium_until',0)).strftime("%d.%m.%Y %H:%M") if u.get("premium_until") else "—"
            txt=(f"👤 Пользователь {target}\n"
                 f"• Премиум до: {exp}")
            return await q.message.reply_text(txt, reply_markup=admin_subs_user_kb(target))
        if cmd == "subs_action" and len(parts) >= 4:
            action=parts[2]; target=int(parts[3]); u=usage_entry(target)
            if action=="add30":
                till=extend_premium_days(target,30)
                return await q.message.reply_text(f"✅ Продлено до {datetime.fromtimestamp(till):%d.%м.%Y %H:%M}", reply_markup=admin_subs_user_kb(target))
            if action=="clear":
                u["premium"]=False; u["premium_until"]=0; persist_all()
                return await q.message.reply_text("✅ Премиум снят.", reply_markup=admin_subs_user_kb(target))

        if cmd == "reload_refs":
            try:
                REF.reload_all()
                return await q.message.reply_text("✅ Справочники обновлены.", reply_markup=admin_main_keyboard())
            except Exception as e:
                return await q.message.reply_text(f"⚠️ Не удалось обновить: {e}", reply_markup=admin_main_keyboard())

async def on_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
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
                await asyncio.sleep(0.03)
            except Forbidden:
                fail += 1
            except Exception:
                fail += 1
        return await update.message.reply_text(f"📣 Готово: отправлено {sent}, ошибок {fail}.", reply_markup=admin_main_keyboard())

# ---------- Flask + сервисные эндпоинты ----------
def start_flask_endpoints(port:int):
    app=Flask(__name__)

    @app.get("/healthz")
    def healthz(): return "ok",200

    th=Thread(target=lambda: app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False))
    th.daemon=True; th.start(); log.info("Flask: /healthz on %s", port)

# ---------- Команды ----------
async def on_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; ensure_user(uid)
    sheets_log_user(uid, getattr(update.effective_user,"username",None))
    await update.message.reply_text("Привет! Пришли фото — сделаю анализ 💄", reply_markup=action_keyboard(uid, context.user_data))
    await update.message.reply_text(get_usage_text(uid))

async def on_ping(update:Update,_): await update.message.reply_text("pong")

# ---------- main ----------
def main():
    app=Application.builder().token(BOT_TOKEN).build()

    # Профиль — диалог
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

    # Фото
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    # Кнопки
    app.add_handler(CallbackQueryHandler(on_callback))

    # Текст (рассылка и проч.)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    start_flask_endpoints(PORT)
    sheets_init()
    try: REF.reload_all()
    except Exception as e: log.warning("RefData init failed: %s", e)

    app.run_polling()

if __name__=="__main__":
    main()
