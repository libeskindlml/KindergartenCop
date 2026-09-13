"""
שכבת גישה למסד הנתונים. SQLite — מספיק ומתאים לשלב ה-POC
(קבוצה אחת, Tenant אחד). ראו מסמך האפיון סעיף 6 למודל הנתונים המלא
של שלב ה-MVP/SaaS; כאן מומש תת-קבוצה רלוונטית ל-POC בלבד.
"""
from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
from datetime import datetime
from typing import Any, Iterator, Optional

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_message_id TEXT UNIQUE,
    account_id INTEGER REFERENCES accounts(id),  -- שיוך רב-דיירים (Multi-tenant): לאיזה חשבון שייכת ההודעה
    group_id TEXT,
    sender_phone TEXT,
    sender_name TEXT,
    msg_type TEXT NOT NULL,           -- text | image | sticker | video | audio | document | location | other
    text TEXT,
    -- NULL עבור הודעות רגילות. 'voice' כאשר text הוא תמלול (לא הטקסט המקורי) של
    -- הודעה קולית שהתקבלה כ-msg_type='audio' — ר' app/transcription.py. msg_type
    -- עצמו לא משתנה (נשאר 'audio'), כך שמקור ההודעה המקורי תמיד נשמר.
    source_type TEXT,
    media_path TEXT,
    media_sha256 TEXT,
    is_forwarded INTEGER DEFAULT 0,
    is_deleted INTEGER DEFAULT 0,
    is_edited INTEGER DEFAULT 0,
    original_text TEXT,
    reply_to_provider_id TEXT,
    sent_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    ai_analysis TEXT,                  -- JSON
    processing_status TEXT DEFAULT 'received'  -- received|analyzed|rules_checked|ai_failed
);
CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_phone, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_account ON messages(account_id, sent_at);

CREATE TABLE IF NOT EXISTS sticker_cache (
    sha256 TEXT PRIMARY KEY,
    description TEXT,
    sentiment TEXT,
    flags TEXT,                        -- JSON
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    rule_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    rule_kind TEXT NOT NULL,           -- deterministic | semantic
    severity TEXT NOT NULL,            -- low | medium | high | critical
    confidence REAL,
    explanation TEXT,
    quoted_evidence TEXT,
    status TEXT DEFAULT 'open',        -- open | handled | false_positive
    feedback TEXT,
    shadow_mode INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_violations_created ON violations(created_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    violation_id INTEGER NOT NULL REFERENCES violations(id),
    contact_name TEXT NOT NULL,
    channel TEXT NOT NULL,             -- whatsapp | email | log_only
    status TEXT NOT NULL,              -- sent | delivered | read | failed | logged
    provider_msg_id TEXT,
    error TEXT,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    msg_count INTEGER DEFAULT 0,
    by_type TEXT,                       -- JSON
    unique_senders INTEGER DEFAULT 0,
    violation_count INTEGER DEFAULT 0,
    by_severity TEXT                    -- JSON
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id),
    date TEXT NOT NULL,
    html_path TEXT,
    sent_to TEXT,
    sent_at TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    owner_name TEXT,
    group_display_name TEXT NOT NULL,
    timezone TEXT DEFAULT 'Asia/Jerusalem',
    strictness_level TEXT DEFAULT 'beinoni',   -- kal | beinoni | machmir (F-5.1)
    onboarding_completed INTEGER DEFAULT 0,
    -- קוד אימות קצר וייחודי שנוצר בסיום האונבורדינג ומוצג למשתמש (F-2.2). הוא
    -- שולח אותו כהודעה ראשונה בקבוצה אחרי הוספת מספר ה-Reader — זה מה שמוכיח
    -- לליבה שהקבוצה הזו שייכת דווקא לחשבון הזה, ולא רק "מי שממתין הכי הרבה
    -- זמן" (ראו get_unbound_account_by_verification_code, main.whatsapp_reader_webhook).
    verification_code TEXT,
    -- מזהה הקבוצה המנוטרת בפועל. לא מוזן ידנית: מתמלא אוטומטית ע"י הליבה
    -- (main.whatsapp_reader_webhook) ברגע שמגיעה הודעה עם קוד האימות התואם, אחרי
    -- שהמשתמש סיים את אשף ההרשמה והוסיף את מספר ה-Reader לקבוצה בפועל.
    monitored_group_id TEXT,
    -- מועד קשירת הקבוצה (=תחילת חלון ה-Shadow Mode של החשבון הזה, F-5.4).
    -- ב-Multi-tenant כל חשבון מתחיל את חלון ה-48 השעות שלו בנפרד, מרגע שהוא
    -- עצמו נקשר לקבוצה שלו — לא מרגע עליית השירות הגלובלי.
    shadow_started_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    phone_e164 TEXT NOT NULL,
    email TEXT,
    min_severity TEXT DEFAULT 'medium',
    channels TEXT DEFAULT '["whatsapp"]',   -- JSON list
    quiet_hours TEXT DEFAULT '{"enabled": false}',  -- JSON
    optin_status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    rule_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT DEFAULT 'medium',
    confidence_threshold REAL DEFAULT 0.8,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS group_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id),
    event_type TEXT NOT NULL,          -- join | leave | remove | subject_change | admin_change
    actor_phone TEXT,
    actor_name TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


@contextlib.contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_verification_code(conn)
        _migrate_source_type(conn)


def now_iso() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------- messages -
def insert_message(**fields: Any) -> Optional[int]:
    """מכניס הודעה. אם provider_message_id כבר קיים — אידמפוטנטי, לא יוצר כפילות (F idempotency)."""
    fields.setdefault("received_at", now_iso())
    ai_analysis = fields.get("ai_analysis")
    if isinstance(ai_analysis, (dict, list)):
        fields["ai_analysis"] = json.dumps(ai_analysis, ensure_ascii=False)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        try:
            cur = conn.execute(
                f"INSERT INTO messages ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # provider_message_id כבר קיים — עדכן טקסט/מדיה במקום ליצור כפילות
            row = conn.execute(
                "SELECT id FROM messages WHERE provider_message_id = ?",
                (fields.get("provider_message_id"),),
            ).fetchone()
            return row["id"] if row else None


def update_message(message_id: int, **fields: Any) -> None:
    if not fields:
        return
    ai_analysis = fields.get("ai_analysis")
    if isinstance(ai_analysis, (dict, list)):
        fields["ai_analysis"] = json.dumps(ai_analysis, ensure_ascii=False)
    set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE messages SET {set_clause} WHERE id = ?",
            list(fields.values()) + [message_id],
        )


def get_message(message_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_message_by_provider_id(provider_message_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE provider_message_id = ?", (provider_message_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_recent_messages(limit: int = 5, before_id: Optional[int] = None) -> list[dict]:
    """N ההודעות האחרונות — משמש כהקשר לניתוח מדיה/חוקים סמנטיים (F-4.3)."""
    with get_conn() as conn:
        if before_id is not None:
            rows = conn.execute(
                "SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]


def list_messages(limit: int = 100, account_id: Optional[int] = None) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute(
                "SELECT * FROM messages WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_messages_since(sender_phone: str, since_iso: str, account_id: Optional[int] = None) -> int:
    """סופר הודעות משולח נתון מאז זמן נתון — מוגבל לחשבון (=לקבוצה) הרלוונטי
    בלבד ב-Multi-tenant, כדי שספירת ספאם/הצפה (F-5.5.1) לא תדלוף בין קבוצות."""
    with get_conn() as conn:
        if account_id is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE sender_phone = ? AND received_at >= ? AND account_id = ?",
                (sender_phone, since_iso, account_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE sender_phone = ? AND received_at >= ?",
                (sender_phone, since_iso),
            ).fetchone()
        return row["c"]


# ------------------------------------------------------------- sticker cache
def get_cached_sticker(sha256: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sticker_cache WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def cache_sticker(sha256: str, description: str, sentiment: str, flags: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sticker_cache (sha256, description, sentiment, flags, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(sha256) DO UPDATE SET description=excluded.description,
                   sentiment=excluded.sentiment, flags=excluded.flags""",
            (sha256, description, sentiment, json.dumps(flags, ensure_ascii=False), now_iso()),
        )


# --------------------------------------------------------------- violations
def insert_violation(**fields: Any) -> int:
    fields.setdefault("created_at", now_iso())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO violations ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid


def last_violation_time(rule_id: str, sender_phone: str, account_id: Optional[int] = None) -> Optional[str]:
    with get_conn() as conn:
        if account_id is not None:
            row = conn.execute(
                """SELECT v.created_at FROM violations v
                   JOIN messages m ON m.id = v.message_id
                   WHERE v.rule_id = ? AND m.sender_phone = ? AND m.account_id = ?
                   ORDER BY v.created_at DESC LIMIT 1""",
                (rule_id, sender_phone, account_id),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT v.created_at FROM violations v
                   JOIN messages m ON m.id = v.message_id
                   WHERE v.rule_id = ? AND m.sender_phone = ?
                   ORDER BY v.created_at DESC LIMIT 1""",
                (rule_id, sender_phone),
            ).fetchone()
        return row["created_at"] if row else None


def list_violations(limit: int = 200, account_id: Optional[int] = None) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute(
                """SELECT v.*, m.text AS message_text, m.sender_name, m.sender_phone, m.msg_type
                   FROM violations v JOIN messages m ON m.id = v.message_id
                   WHERE m.account_id = ?
                   ORDER BY v.id DESC LIMIT ?""",
                (account_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT v.*, m.text AS message_text, m.sender_name, m.sender_phone, m.msg_type
                   FROM violations v JOIN messages m ON m.id = v.message_id
                   ORDER BY v.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_violation_feedback(violation_id: int, status: str, feedback: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE violations SET status = ?, feedback = ? WHERE id = ?",
            (status, feedback, violation_id),
        )


# ------------------------------------------------------------------ alerts
def insert_alert(**fields: Any) -> int:
    fields.setdefault("sent_at", now_iso())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO alerts ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid


def list_alerts(limit: int = 200, account_id: Optional[int] = None) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute(
                """SELECT a.* FROM alerts a
                   JOIN violations v ON v.id = a.violation_id
                   JOIN messages m ON m.id = v.message_id
                   WHERE m.account_id = ?
                   ORDER BY a.id DESC LIMIT ?""",
                (account_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


# ------------------------------------------------------------------- stats
def upsert_daily_stats(date: str, **fields: Any) -> None:
    by_type = fields.get("by_type")
    if isinstance(by_type, dict):
        fields["by_type"] = json.dumps(by_type, ensure_ascii=False)
    by_severity = fields.get("by_severity")
    if isinstance(by_severity, dict):
        fields["by_severity"] = json.dumps(by_severity, ensure_ascii=False)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT date FROM daily_stats WHERE date = ?", (date,)
        ).fetchone()
        if existing:
            set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
            conn.execute(
                f"UPDATE daily_stats SET {set_clause} WHERE date = ?",
                list(fields.values()) + [date],
            )
        else:
            fields["date"] = date
            cols = ", ".join(fields.keys())
            placeholders = ", ".join(["?"] * len(fields))
            conn.execute(
                f"INSERT INTO daily_stats ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )


def get_messages_for_date(date: str, account_id: Optional[int] = None) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute(
                "SELECT * FROM messages WHERE substr(received_at, 1, 10) = ? AND account_id = ? ORDER BY received_at",
                (date, account_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE substr(received_at, 1, 10) = ? ORDER BY received_at",
                (date,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_violations_for_date(date: str, account_id: Optional[int] = None) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute(
                """SELECT v.*, m.sender_name FROM violations v
                   JOIN messages m ON m.id = v.message_id
                   WHERE substr(v.created_at, 1, 10) = ? AND m.account_id = ?""",
                (date, account_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT v.*, m.sender_name FROM violations v
                   JOIN messages m ON m.id = v.message_id
                   WHERE substr(v.created_at, 1, 10) = ?""",
                (date,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def insert_report(**fields: Any) -> int:
    fields.setdefault("sent_at", now_iso())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO reports ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid


def list_reports(account_id: Optional[int] = None, limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute(
                "SELECT * FROM reports WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def insert_group_event(**fields: Any) -> int:
    fields.setdefault("created_at", now_iso())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO group_events ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid


# --------------------------------------------------------------------- meta
def get_meta(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_or_init_started_at() -> str:
    """נקודת ההתחלה של המערכת — לחישוב חלון Shadow Mode (F-5.4)."""
    val = get_meta("started_at")
    if val:
        return val
    val = now_iso()
    set_meta("started_at", val)
    return val


# --------------------------------------------------------------- accounts --
VERIFICATION_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # בלי 0/O/1/I/L — פחות טעויות הקלדה
VERIFICATION_CODE_LENGTH = 6


def _random_verification_code() -> str:
    return "".join(secrets.choice(VERIFICATION_CODE_ALPHABET) for _ in range(VERIFICATION_CODE_LENGTH))


def _unique_verification_code(conn: sqlite3.Connection) -> str:
    while True:
        code = _random_verification_code()
        exists = conn.execute("SELECT 1 FROM accounts WHERE verification_code = ?", (code,)).fetchone()
        if not exists:
            return code


def generate_unique_verification_code() -> str:
    """קוד אימות ייחודי לחשבון חדש — נקרא לפני create_account וכולל אותו בשדותיו
    (ראו main.onboarding_complete). ראו גם get_unbound_account_by_verification_code."""
    with get_conn() as conn:
        return _unique_verification_code(conn)


def _migrate_verification_code(conn: sqlite3.Connection) -> None:
    """הוספת עמודת verification_code לחשבונות שנוצרו לפני שהיא נוספה לסכימה —
    CREATE TABLE IF NOT EXISTS לא משנה טבלה קיימת, אז חשבונות ישנים היו נשארים
    בלי העמודה הזו בכלל. גם משלים קוד לחשבונות ישנים שעדיין אין להם אחד."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(accounts)")]
    if "verification_code" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN verification_code TEXT")
    missing = conn.execute("SELECT id FROM accounts WHERE verification_code IS NULL").fetchall()
    for row in missing:
        code = _unique_verification_code(conn)
        conn.execute("UPDATE accounts SET verification_code = ? WHERE id = ?", (code, row["id"]))


def _migrate_source_type(conn: sqlite3.Connection) -> None:
    """הוספת עמודת source_type להודעות שנוצרו לפני שהיא נוספה לסכימה (ר' הערה
    ב-SCHEMA למעלה) — CREATE TABLE IF NOT EXISTS לא משנה טבלה קיימת."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)")]
    if "source_type" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN source_type TEXT")


def create_account(**fields: Any) -> int:
    fields.setdefault("created_at", now_iso())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO accounts ({cols}) VALUES ({placeholders})", list(fields.values()))
        return cur.lastrowid


def get_account(account_id: Optional[int] = None) -> Optional[dict]:
    """Multi-tenant: נקרא כמעט תמיד עם account_id מפורש. ללא account_id (שימוש ישן
    ונדיר) מחזיר את החשבון הראשון שנמצא — לא לשימוש בזרימות חדשות."""
    with get_conn() as conn:
        if account_id is not None:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
        return _row_to_dict(row) if row else None


def get_account_by_email(email: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        return _row_to_dict(row) if row else None


def mark_onboarding_completed(account_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET onboarding_completed = 1 WHERE id = ?", (account_id,))


def set_monitored_group_id(account_id: int, group_id: str) -> None:
    """קושר לחשבון את מזהה הקבוצה שזוהתה אוטומטית באירוע הוואטסאפ הראשון שהתקבל
    לאחר סיום האונבורדינג (ראו main.whatsapp_reader_webhook). נכתב פעם אחת בלבד —
    קריאות חוזרות (אם בכל זאת מתבצעות) פשוט לא ישנו ערך שכבר נקבע. באותו הרגע
    גם ננעל shadow_started_at — תחילת חלון ה-Shadow Mode האישי של החשבון הזה."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET monitored_group_id = ?, shadow_started_at = ? "
            "WHERE id = ? AND monitored_group_id IS NULL",
            (group_id, now_iso(), account_id),
        )


def list_accounts() -> list[dict]:
    """כל החשבונות הרשומים (Multi-tenant) — משמש את פאנל האדמין (CRM)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
        return [_row_to_dict(r) for r in rows]


def get_account_by_group_id(group_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE monitored_group_id = ?", (group_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_unbound_account_by_verification_code(code: str) -> Optional[dict]:
    """מאתר חשבון שסיים אונבורדינג ועדיין לא נקשר לקבוצה, לפי קוד האימות שהוצג לו
    בסוף ההרשמה. זהו מנגנון האימות בפועל: ה-webhook (main.whatsapp_reader_webhook)
    קושר קבוצה לא מוכרת לחשבון רק כשההודעה הראשונה ממנה תואמת בדיוק לקוד של אותו
    חשבון — לא לפי סדר הרשמה בלבד (השוו ל-get_oldest_unbound_account למטה, שכבר
    לא משמשת לקישור אוטומטי מסיבה זו)."""
    if not code:
        return None
    normalized = code.strip().upper()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE onboarding_completed = 1 AND monitored_group_id IS NULL "
            "AND verification_code = ?",
            (normalized,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_oldest_unbound_account() -> Optional[dict]:
    """החשבון הישן ביותר שסיים אונבורדינג אך עדיין לא נקשר לאף קבוצה. הערה: מאז
    הוספת האימות בקוד (get_unbound_account_by_verification_code) הפונקציה הזו כבר
    לא משמשת לקישור אוטומטי של קבוצות — נשארת ככלי עזר אפשרי לפאנל האדמין בלבד
    (למשל להציג "מי הכי ותיק שעדיין ממתין לאימות")."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE onboarding_completed = 1 AND monitored_group_id IS NULL "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row) if row else None


def delete_account(account_id: int) -> None:
    """מחיקת חשבון וכל הנתונים התלויים בו — לשימוש בפאנל האדמין בלבד (ניקוי חשבונות בדיקה)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM alert_contacts WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM custom_rules WHERE account_id = ?", (account_id,))
        conn.execute("UPDATE messages SET account_id = NULL WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


# --------------------------------------------------------- alert contacts --
def insert_alert_contact_row(**fields: Any) -> int:
    fields.setdefault("created_at", now_iso())
    for key in ("channels", "quiet_hours"):
        if isinstance(fields.get(key), (dict, list)):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO alert_contacts ({cols}) VALUES ({placeholders})", list(fields.values()))
        return cur.lastrowid


def list_alert_contacts_db(account_id: Optional[int] = None) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute("SELECT * FROM alert_contacts WHERE account_id = ?", (account_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM alert_contacts").fetchall()
        result = []
        for r in rows:
            d = _row_to_dict(r)
            d["channels"] = json.loads(d.get("channels") or "[]")
            d["quiet_hours"] = json.loads(d.get("quiet_hours") or "{}")
            result.append(d)
        return result


# ------------------------------------------------------------- custom rules
def insert_custom_rule(**fields: Any) -> int:
    fields.setdefault("created_at", now_iso())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO custom_rules ({cols}) VALUES ({placeholders})", list(fields.values()))
        return cur.lastrowid


def list_custom_rules_db(account_id: Optional[int] = None) -> list[dict]:
    with get_conn() as conn:
        if account_id is not None:
            rows = conn.execute("SELECT * FROM custom_rules WHERE account_id = ?", (account_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM custom_rules").fetchall()
        return [_row_to_dict(r) for r in rows]
