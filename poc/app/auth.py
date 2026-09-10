"""
עזרי אימות (F-1.1): גיבוב/אימות סיסמאות משתמש עם Argon2, וכן בדיקת סיסמת
האדמין היחידה (ADMIN_PASSWORD, ב-.env) לפאנל ה-CRM. Multi-tenant: כל חשבון
משתמש מתחבר בנפרד (ראו main.py: login/logout) ומקבל session cookie חתום
(Starlette SessionMiddleware, ראו main.py) עם account_id — כך שכל משתמש
רואה רק את הנתונים שלו.
"""
from __future__ import annotations

import hmac

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()


def hash_password(raw_password: str) -> str:
    return _hasher.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, raw_password)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001
        return False


def verify_admin_password(raw_password: str) -> bool:
    """השוואה בזמן קבוע (hmac.compare_digest) מול ADMIN_PASSWORD מה-.env,
    כדי לא לחשוף תזמון-השוואה (timing attack) — גם אם זה POC."""
    expected = get_settings().admin_password
    if not expected:
        return False
    return hmac.compare_digest(raw_password, expected)
