# refdata.py
import os, json, time
from typing import Any, Dict, List, Optional
import gspread
from google.oauth2.service_account import Credentials

STATE_DIR = os.getenv("STATE_DIR", "./state")
os.makedirs(STATE_DIR, exist_ok=True)

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID") or os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
if not SPREADSHEET_ID:
    print("[refdata] WARNING: SPREADSHEET_ID/GOOGLE_SHEETS_SPREADSHEET_ID is empty")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

def _gc():
    # два способа: путь к файлу или base64 в переменной (как у тебя сейчас)
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "")
    creds_b64  = os.getenv("GOOGLE_SHEETS_CREDS", "")
    if creds_path and os.path.exists(creds_path):
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    elif creds_b64:
        info = json.loads((os.getenv("GOOGLE_SHEETS_CREDS") or "").encode("utf-8").decode("utf-8"))
        # если b64 — декодируем
        try:
            info = json.loads((__import__("base64").b64decode(os.getenv("GOOGLE_SHEETS_CREDS")).decode("utf-8")))
        except Exception:
            pass
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        raise RuntimeError("No Google credentials provided (GOOGLE_CREDENTIALS_PATH or GOOGLE_SHEETS_CREDS)")
    return gspread.authorize(creds)

def _open_ws(client, title: str):
    sh = client.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(title)

def _read_table(ws) -> List[Dict[str, Any]]:
    rows = ws.get_all_records(numericise_ignore=["all"])
    def norm(v):
        if isinstance(v, str):
            s = v.strip()
            if s.upper() in ("TRUE","FALSE"):
                return s.upper() == "TRUE"
            return s
        return v
    return [{k: norm(v) for k, v in row.items()} for row in rows]

def _load_json_fallback(name: str, default: Any):
    path = os.path.join(STATE_DIR, f"ref_{name}.json")
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json_fallback(name: str, data: Any):
    path = os.path.join(STATE_DIR, f"ref_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

class RefData:
    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._ts: Dict[str, float] = {}
        self.ttl_sec = 300  # 5 минут

    def _expired(self, key: str) -> bool:
        return time.time() - self._ts.get(key, 0) > self.ttl_sec

    def _load_sheet(self, title: str) -> List[Dict[str, Any]]:
        client = _gc()
        ws = _open_ws(client, title)
        data = _read_table(ws)
        self._cache[title] = data
        self._ts[title] = time.time()
        _save_json_fallback(title, data)
        return data

    def _get(self, title: str) -> List[Dict[str, Any]]:
        if title in self._cache and not self._expired(title):
            return self._cache[title]
        try:
            return self._load_sheet(title)
        except Exception as e:
            print(f"[refdata] get fallback {title}: {e}")
            data = _load_json_fallback(title, [])
            self._cache[title] = data
            self._ts[title] = time.time()
            return data

    # публичные апи
    def reload_all(self) -> None:
        for title in ("admins", "limits_prices", "catalog", "messages", "feature_flags"):
            try:
                self._load_sheet(title)
            except Exception as e:
                print(f"[refdata] reload {title} failed: {e}")

    def is_admin(self, user_id: int) -> bool:
        rows = self._get("admins")
        for r in rows:
            if str(r.get("user_id")) == str(user_id) and bool(r.get("is_active", False)):
                return True
        return False

    def get_limit(self, key: str, default: Optional[int] = None) -> int:
        rows = self._get("limits_prices")
        for r in rows:
            if str(r.get("key")) == key:
                val = r.get("value")
                try:
                    return int(val)
                except Exception:
                    return default if default is not None else 0
        return default if default is not None else 0

    def get_price(self, key: str, default: Optional[int] = None) -> int:
        return self.get_limit(key, default)

    def get_catalog(self, active_only: bool = True) -> List[Dict[str, Any]]:
        rows = self._get("catalog")
        out = []
        for r in rows:
            if active_only and not bool(r.get("is_active", True)):
                continue
            tags = r.get("tags") or ""
            r["tags"] = [t.strip() for t in str(tags).split(";") if t.strip()]
            try:
                r["priority"] = int(r.get("priority", 0))
            except Exception:
                r["priority"] = 0
            out.append(r)
        return sorted(out, key=lambda x: x.get("priority", 0), reverse=True)

    def get_sku(self, sku: str):
        for item in self.get_catalog(active_only=False):
            if str(item.get("sku")).strip() == sku:
                return item
        return None

    def msg(self, key: str, locale: str = "ru", default: Optional[str] = None) -> str:
        rows = self._get("messages")
        for r in rows:
            if str(r.get("key")) == key and str(r.get("locale","ru")) == locale:
                return str(r.get("text",""))
        return default if default is not None else key

    def feature_enabled(self, flag: str, default: bool = False) -> bool:
        rows = self._get("feature_flags")
        for r in rows:
            if str(r.get("flag")) == flag:
                return bool(r.get("enabled", default))
        return default

REF = RefData()
