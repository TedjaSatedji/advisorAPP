"""
XPense — A refined financial tracking application.
Flask + SQLite + Jinja2. Dark-mode editorial dashboard.
"""

import sqlite3
import os
import json
import uuid
import re
import secrets
import hashlib
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, g, session
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None


def _load_env_fallback(env_path):
    """Lightweight .env parser for bootstrapping when python-dotenv is unavailable."""
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # Keep startup resilient; the required SECRET_KEY check runs below.
        return

try:
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore
except Exception:
    genai = None
    types = None

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "xpense.db")

if load_dotenv is not None:
    load_dotenv(os.path.join(BASE_DIR, ".env"))
else:
    _load_env_fallback(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "").strip()
if not app.secret_key:
    raise RuntimeError("Missing required SECRET_KEY environment variable.")

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
MODEL = "gemma-3-27b-it"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", MODEL)
ENTRY_UPLOAD_REL_DIR = os.path.join("uploads", "entries")
ENTRY_UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads", "entries")
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").strip().rstrip("/")

MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", "smtp.gmail.com").strip()
MAIL_SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587") or "587")
MAIL_SMTP_USER = os.environ.get("MAIL_SMTP_USER", "").strip()
MAIL_SMTP_PASSWORD = os.environ.get("MAIL_SMTP_PASSWORD", "").strip()
MAIL_SMTP_USE_TLS = os.environ.get("MAIL_SMTP_USE_TLS", "1") == "1"
MAIL_SMTP_TIMEOUT_SECONDS = int(os.environ.get("MAIL_SMTP_TIMEOUT_SECONDS", "15") or "15")
MAIL_FROM_EMAIL = os.environ.get("MAIL_FROM_EMAIL", MAIL_SMTP_USER).strip()
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "XPense Support").strip()
MAIL_DEBUG_SHOW_RESET_LINK = os.environ.get("MAIL_DEBUG_SHOW_RESET_LINK", "0") == "1"
MAIL_PASSWORD_RESET_SUBJECT = os.environ.get("MAIL_PASSWORD_RESET_SUBJECT", "XPense Password Reset").strip()
MAIL_PASSWORD_RESET_EXPIRY_MINUTES = int(os.environ.get("MAIL_PASSWORD_RESET_EXPIRY_MINUTES", "60") or "60")

MAIL_SMTP_ENABLED = bool(
    MAIL_SMTP_HOST and MAIL_SMTP_PORT and MAIL_SMTP_USER and MAIL_SMTP_PASSWORD and MAIL_FROM_EMAIL
)


def current_user_id():
    user_id = session.get("user_id")
    if isinstance(user_id, int):
        return user_id
    return None


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            flash("Please log in first.", "error")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def _safe_next_path(candidate):
    value = str(candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return ""


@app.before_request
def load_user_and_require_auth():
    endpoint = (request.endpoint or "").strip()
    public_endpoints = {
        "login",
        "register",
        "forgot_password",
        "reset_password",
        "static",
    }

    user_id = current_user_id()
    g.current_user = None
    if user_id:
        db = get_db()
        g.current_user = db.execute(
            "SELECT id, email, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if g.current_user is None:
            session.clear()
        else:
            ensure_user_default_categories(g.current_user["id"])

    if endpoint.startswith("static") or endpoint in public_endpoints:
        return None

    if g.current_user is None:
        return redirect(url_for("login", next=request.path))

    return None


def _extract_json_object(text):
    """Extract a JSON object from plain text or fenced markdown output."""
    if not text:
        return None

    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        for p in parts:
            p = p.strip()
            if p and not p.lower().startswith("json"):
                raw = p
                break
            if p.lower().startswith("json"):
                raw = p[4:].strip()
                break

    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                return None
    return None


def _extract_json_array(text):
    """Extract a JSON array from plain text or fenced markdown output."""
    if not text:
        return None

    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        for p in parts:
            p = p.strip()
            if p and not p.lower().startswith("json"):
                raw = p
                break
            if p.lower().startswith("json"):
                raw = p[4:].strip()
                break

    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                return None
    return None


def _coerce_amount(value):
    """Convert mixed amount formats (e.g. 12,345.67 or 12.345,67) to float."""
    if isinstance(value, (int, float)):
        return abs(float(value))

    raw = str(value or "").strip()
    if not raw:
        return 0.0

    # Keep only numeric separators/sign after removing currency text.
    cleaned = re.sub(r"[^0-9,.-]", "", raw)
    if not cleaned:
        return 0.0

    # Normalize decimal separator when both comma and dot are present.
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Treat comma as decimal only when likely decimal precision.
        if cleaned.count(",") == 1 and len(cleaned.split(",")[-1]) in (1, 2):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return abs(float(cleaned))
    except Exception:
        return 0.0


def _best_guess_total_from_text(text):
    """Heuristic fallback to detect payable total from plain receipt text."""
    if not text:
        return 0.0

    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    preferred_patterns = [
        r"(?i)grand\s*total",
        r"(?i)amount\s*due",
        r"(?i)total\s*due",
        r"(?i)total\s*bayar",
        r"(?i)jumlah\s*bayar",
        r"(?i)total\s*payment",
        r"(?i)net\s*total",
        r"(?i)total",
    ]
    money_pattern = r"[-+]?\d[\d.,]*"

    def find_amount_in_line(line):
        matches = re.findall(money_pattern, line)
        if not matches:
            return 0.0
        # Receipts often print the payable amount as the last number on the line.
        return _coerce_amount(matches[-1])

    for pat in preferred_patterns:
        for line in reversed(lines):
            if re.search(pat, line):
                amount = find_amount_in_line(line)
                if amount > 0:
                    return amount

    # Last resort: choose the largest amount visible.
    all_numbers = re.findall(money_pattern, text)
    if not all_numbers:
        return 0.0
    return max((_coerce_amount(n) for n in all_numbers), default=0.0)


def _normalize_text(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())).strip()


def _is_generic_category(name):
    normalized = _normalize_text(name)
    return normalized in {
        "makanan",
        "minuman",
        "food",
        "drink",
        "groceries",
        "restaurant",
        "cafe",
        "others",
        "other",
        "misc",
        "miscellaneous",
    }


def _is_allowed_specific_category(category_name, allowed_categories):
    normalized = _normalize_text(category_name)
    if not normalized:
        return False
    return any(_normalize_text(item) == normalized for item in allowed_categories)


def _category_supported_by_text(category_name, text):
    """Only accept a category if the raw model text contains the category or a strong cue."""
    if not category_name or not text:
        return False

    category_norm = _normalize_text(category_name)
    raw_norm = _normalize_text(text)

    if category_norm and category_norm in raw_norm:
        return True

    cues = {
        "makanan": ["food", "meal", "rice", "lunch", "dinner", "breakfast", "restaurant", "cafe"],
        "minuman": ["drink", "beverage", "coffee", "tea", "juice", "water"],
        "salary": ["salary", "payroll", "wage", "gaji"],
        "bank": ["bank", "transfer", "atm", "withdrawal"],
        "cash": ["cash", "tunai"],
        "freelance": ["freelance", "invoice", "project"],
    }

    for key, words in cues.items():
        if category_norm == key or category_norm.endswith(" " + key):
            return any(word in raw_norm for word in words)

    return False


def _category_keyword_map(tx_type):
    if tx_type == "income":
        return {
            "salary": ["salary", "payroll", "wage", "gaji"],
            "bank": ["bank", "transfer", "atm", "withdrawal", "deposit"],
            "cash": ["cash", "tunai"],
            "freelance": ["freelance", "invoice", "project", "client"],
        }

    return {
        "makanan": [
            "food", "meal", "rice", "lunch", "dinner", "breakfast",
            "restaurant", "cafe", "warung", "warteg", "bakso", "mie",
            "soto", "ayam", "nasi", "ayam geprek", "burger", "pizza",
        ],
        "minuman": [
            "drink", "beverage", "coffee", "tea", "juice", "water",
            "kopi", "latte", "espresso", "cappuccino", "starbucks", "boba",
        ],
    }


def _guess_likely_category_match(store_name, category_name, raw_text, tx_type):
    """Infer a likely category from the AI text and receipt clues without using the existing category list."""
    raw_norm = _normalize_text(raw_text)
    store_norm = _normalize_text(store_name)
    category_norm = _normalize_text(category_name)
    keyword_map = _category_keyword_map(tx_type)

    best_category = ""
    best_score = 0

    for candidate, keywords in keyword_map.items():
        score = 0

        if category_norm == candidate:
            score += 6
        if category_norm and candidate in category_norm:
            score += 4
        if candidate in store_norm:
            score += 3
        if candidate in raw_norm:
            score += 2

        for keyword in keywords:
            if keyword in store_norm:
                score += 4
            if keyword in raw_norm:
                score += 2

        if score > best_score:
            best_score = score
            best_category = candidate

    if best_score <= 0:
        return ""
    return best_category


def _extract_transaction_fields_from_text(text):
    """Best-effort extraction when the model returns non-JSON text."""
    if not text:
        return {}

    raw = text.strip()

    amount = 0.0
    store_name = ""
    date_str = ""
    category_name = ""
    description = ""

    amount_match = re.search(
        r'(?i)(?:"amount"\s*:\s*"?([^"\n,}]+)|\b(?:total|amount|grand\s*total)\b[^0-9\-]*([-+]?\d[\d.,]*))',
        raw,
    )
    if amount_match:
        amount = _coerce_amount(amount_match.group(1) or amount_match.group(2) or "")

    store_match = re.search(
        r'(?i)"(?:store_name|merchant|vendor|shop|store)"\s*:\s*"([^"]+)"',
        raw,
    )
    if not store_match:
        store_match = re.search(r'(?im)^\s*(?:store|merchant|vendor|shop)\s*[:=-]\s*(.+)$', raw)
    if store_match:
        store_name = (store_match.group(1) or "").strip()

    category_match = re.search(r'(?i)"(?:category_name|category)"\s*:\s*"([^"]+)"', raw)
    if not category_match:
        category_match = re.search(r'(?im)^\s*category\s*[:=-]\s*(.+)$', raw)
    if category_match:
        category_name = (category_match.group(1) or "").strip()

    description_match = re.search(r'(?i)"description"\s*:\s*"([^"]+)"', raw)
    if not description_match:
        description_match = re.search(r'(?im)^\s*description\s*[:=-]\s*(.+)$', raw)
    if description_match:
        description = (description_match.group(1) or "").strip()

    iso_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', raw)
    if iso_match:
        date_str = iso_match.group(1)
    else:
        slash_match = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b', raw)
        if slash_match:
            d, m, y = slash_match.groups()
            y = f"20{y}" if len(y) == 2 else y
            try:
                date_str = datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
            except Exception:
                date_str = ""

    return {
        "amount": amount,
        "store_name": store_name,
        "date": date_str,
        "category_name": category_name,
        "description": description,
    }


def _save_entry_image(uploaded_file):
    """Save an uploaded image and return its static-relative path."""
    if uploaded_file is None or not uploaded_file.filename:
        return None

    filename = secure_filename(uploaded_file.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None

    os.makedirs(ENTRY_UPLOAD_DIR, exist_ok=True)
    saved_name = f"{uuid.uuid4().hex}{ext}"
    full_path = os.path.join(ENTRY_UPLOAD_DIR, saved_name)
    uploaded_file.save(full_path)
    return f"{ENTRY_UPLOAD_REL_DIR.replace(os.sep, '/')}/{saved_name}"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Open a new database connection per-request (stored on `g`)."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create all tables and seed default data on first run."""
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT    NOT NULL UNIQUE,
            password_hash   TEXT    NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(user_id, name),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            amount      REAL    NOT NULL,
            description TEXT    NOT NULL DEFAULT '',
            date        TEXT    NOT NULL,
            category_id INTEGER NOT NULL,
            receipt_image TEXT  DEFAULT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS wishlist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            price       REAL    NOT NULL DEFAULT 0,
            priority    TEXT    NOT NULL DEFAULT 'medium',
            notes       TEXT    DEFAULT '',
            purchased   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            purchased_at TEXT   DEFAULT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS income_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(user_id, name),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS income (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            amount          REAL    NOT NULL,
            description     TEXT    NOT NULL DEFAULT '',
            date            TEXT    NOT NULL,
            category_id     INTEGER NOT NULL,
            receipt_image   TEXT    DEFAULT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (category_id) REFERENCES income_categories(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            token_hash      TEXT    NOT NULL UNIQUE,
            expires_at      TEXT    NOT NULL,
            used_at         TEXT    DEFAULT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)

    # Lightweight migration for older DBs created before receipt_image columns existed.
    expense_cols = [r[1] for r in db.execute("PRAGMA table_info(expenses)").fetchall()]
    if "receipt_image" not in expense_cols:
        db.execute("ALTER TABLE expenses ADD COLUMN receipt_image TEXT DEFAULT NULL")
    if "user_id" not in expense_cols:
        db.execute("ALTER TABLE expenses ADD COLUMN user_id INTEGER")

    income_cols = [r[1] for r in db.execute("PRAGMA table_info(income)").fetchall()]
    if "receipt_image" not in income_cols:
        db.execute("ALTER TABLE income ADD COLUMN receipt_image TEXT DEFAULT NULL")
    if "user_id" not in income_cols:
        db.execute("ALTER TABLE income ADD COLUMN user_id INTEGER")

    wishlist_cols = [r[1] for r in db.execute("PRAGMA table_info(wishlist)").fetchall()]
    if "user_id" not in wishlist_cols:
        db.execute("ALTER TABLE wishlist ADD COLUMN user_id INTEGER")

    _migrate_category_tables_to_user_scope(db)
    db.commit()

    db.close()


def _migrate_category_tables_to_user_scope(db):
    """Migrate legacy global categories into user-owned categories."""

    def _is_user_scoped(table_name):
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table_name})").fetchall()]
        if "user_id" not in cols:
            return False
        create_sql_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        create_sql = (create_sql_row[0] or "") if create_sql_row else ""
        return "UNIQUE(user_id, name)" in create_sql or "UNIQUE (user_id, name)" in create_sql

    def _migrate_single(table_name, tx_table, tx_pk_col, fallback_name):
        if _is_user_scoped(table_name):
            return

        first_user = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        default_user_id = first_user[0] if first_user else None
        tx_count = db.execute(f"SELECT COUNT(*) FROM {tx_table}").fetchone()[0]
        if tx_count > 0 and default_user_id is None:
            # Delay migration until at least one user exists.
            return

        db.execute("PRAGMA foreign_keys=OFF")

        db.execute(f"DROP TABLE IF EXISTS {table_name}_new")
        db.execute(
            f"""
            CREATE TABLE {table_name}_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )

        tx_rows = db.execute(
            f"""
            SELECT t.{tx_pk_col} AS tx_id,
                   COALESCE(t.user_id, 0) AS tx_user_id,
                   COALESCE(c.name, ?) AS cat_name,
                   COALESCE(c.created_at, datetime('now','localtime')) AS cat_created_at
            FROM {tx_table} t
            LEFT JOIN {table_name} c ON c.id = t.category_id
            ORDER BY t.{tx_pk_col}
            """,
            (fallback_name,),
        ).fetchall()

        category_id_map = {}
        for tx_id, tx_user_id, cat_name, cat_created_at in tx_rows:
            resolved_user_id = tx_user_id if int(tx_user_id or 0) > 0 else default_user_id
            if resolved_user_id is None:
                continue

            key = (int(resolved_user_id), str(cat_name or fallback_name).strip() or fallback_name)
            new_cat_id = category_id_map.get(key)
            if new_cat_id is None:
                existing = db.execute(
                    f"SELECT id FROM {table_name}_new WHERE user_id = ? AND name = ?",
                    (key[0], key[1]),
                ).fetchone()
                if existing:
                    new_cat_id = existing[0]
                else:
                    cur = db.execute(
                        f"INSERT INTO {table_name}_new (user_id, name, created_at) VALUES (?, ?, ?)",
                        (key[0], key[1], cat_created_at),
                    )
                    new_cat_id = cur.lastrowid
                category_id_map[key] = new_cat_id

            db.execute(
                f"UPDATE {tx_table} SET category_id = ? WHERE {tx_pk_col} = ?",
                (new_cat_id, tx_id),
            )

        db.execute(f"DROP TABLE {table_name}")
        db.execute(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")
        db.execute("PRAGMA foreign_keys=ON")

    _migrate_single("categories", "expenses", "id", "Uncategorized")
    _migrate_single("income_categories", "income", "id", "Other")


def claim_legacy_records(user_id):
    """Assign old rows created before auth migration to the first registered user."""
    db = get_db()
    already_claimed = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM expenses WHERE user_id IS NOT NULL) +
            (SELECT COUNT(*) FROM income WHERE user_id IS NOT NULL) +
            (SELECT COUNT(*) FROM wishlist WHERE user_id IS NOT NULL) AS claimed_count
        """
    ).fetchone()["claimed_count"]

    if int(already_claimed or 0) > 0:
        return

    db.execute("UPDATE expenses SET user_id = ? WHERE user_id IS NULL", (user_id,))
    db.execute("UPDATE income SET user_id = ? WHERE user_id IS NULL", (user_id,))
    db.execute("UPDATE wishlist SET user_id = ? WHERE user_id IS NULL", (user_id,))
    db.commit()


def _utc_now_string():
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _utc_expiry_string(minutes_from_now):
    return (datetime.utcnow() + timedelta(minutes=minutes_from_now)).replace(microsecond=0).isoformat()


def _hash_reset_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def ensure_user_default_categories(user_id):
    if not user_id:
        return

    db = get_db()
    for name in ("Makanan", "Minuman"):
        db.execute(
            "INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )

    for name in ("Bank", "Cash", "Salary", "Freelance"):
        db.execute(
            "INSERT OR IGNORE INTO income_categories (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )

    db.commit()


def _build_absolute_url(path):
    relative = str(path or "").strip()
    if not relative.startswith("/"):
        relative = "/" + relative
    return APP_BASE_URL + relative


def _send_email_smtp(to_email, subject, text_body):
    if not MAIL_SMTP_ENABLED:
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM_EMAIL}>" if MAIL_FROM_NAME else MAIL_FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(text_body)

    context = ssl.create_default_context()

    try:
        with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT, timeout=MAIL_SMTP_TIMEOUT_SECONDS) as server:
            if MAIL_SMTP_USE_TLS:
                server.starttls(context=context)
            server.login(MAIL_SMTP_USER, MAIL_SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as exc:
        app.logger.exception("Failed to send SMTP email: %s", exc)
        return False


def _send_password_reset_email(to_email, reset_url):
    lines = [
        "We received a request to reset your XPense password.",
        "",
        "Use this link to reset your password:",
        reset_url,
        "",
        f"This link expires in {MAIL_PASSWORD_RESET_EXPIRY_MINUTES} minutes and can be used once.",
        "",
        "If you did not request this reset, you can safely ignore this email.",
    ]
    return _send_email_smtp(
        to_email=to_email,
        subject=MAIL_PASSWORD_RESET_SUBJECT,
        text_body="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Template context helpers
# ---------------------------------------------------------------------------

@app.context_processor
def inject_now():
    return {
        "now": datetime.now(),
        "today": date.today().isoformat(),
        "current_user": g.get("current_user"),
    }


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.get("current_user") is not None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        next_url = _safe_next_path(request.form.get("next", ""))

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("login.html", next_url=next_url)

        db = get_db()
        user = db.execute(
            "SELECT id, email, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", next_url=next_url)

        session.clear()
        session["user_id"] = user["id"]
        claim_legacy_records(user["id"])
        ensure_user_default_categories(user["id"])
        flash("Logged in successfully.", "success")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("dashboard"))

    return render_template("login.html", next_url=_safe_next_path(request.args.get("next", "")))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.get("current_user") is not None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("register.html")

        if password != password_confirm:
            flash("Password confirmation does not match.", "error")
            return render_template("register.html")

        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email, generate_password_hash(password)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Email already registered.", "error")
            return render_template("register.html")

        session.clear()
        session["user_id"] = cursor.lastrowid
        claim_legacy_records(cursor.lastrowid)
        ensure_user_default_categories(cursor.lastrowid)
        flash("Account created.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if g.get("current_user") is not None:
        return redirect(url_for("dashboard"))

    reset_link = ""

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Email is required.", "error")
            return render_template("forgot_password.html", reset_link=reset_link)

        db = get_db()
        user = db.execute(
            "SELECT id, email FROM users WHERE email = ?",
            (email,),
        ).fetchone()

        if user is not None:
            raw_token = secrets.token_urlsafe(32)
            token_hash = _hash_reset_token(raw_token)
            expires_at = _utc_expiry_string(MAIL_PASSWORD_RESET_EXPIRY_MINUTES)
            db.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = ?",
                (user["id"],),
            )
            db.execute(
                "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user["id"], token_hash, expires_at),
            )
            db.commit()
            reset_link = _build_absolute_url(url_for("reset_password", token=raw_token))

            sent = _send_password_reset_email(user["email"], reset_link)
            if not sent and MAIL_DEBUG_SHOW_RESET_LINK:
                flash("SMTP is unavailable. Debug reset link is shown below.", "error")

        flash("If your email exists, a password reset email has been sent.", "success")
        return render_template(
            "forgot_password.html",
            reset_link=reset_link if MAIL_DEBUG_SHOW_RESET_LINK else "",
            mail_enabled=MAIL_SMTP_ENABLED,
        )

    return render_template(
        "forgot_password.html",
        reset_link=reset_link if MAIL_DEBUG_SHOW_RESET_LINK else "",
        mail_enabled=MAIL_SMTP_ENABLED,
    )


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if g.get("current_user") is not None:
        return redirect(url_for("dashboard"))

    token_hash = _hash_reset_token(token)
    db = get_db()
    row = db.execute(
        """
        SELECT prt.id, prt.user_id, prt.expires_at, prt.used_at, u.email
        FROM password_reset_tokens prt
        JOIN users u ON prt.user_id = u.id
        WHERE prt.token_hash = ?
        """,
        (token_hash,),
    ).fetchone()

    now_str = _utc_now_string()
    if row is None or row["used_at"] is not None or str(row["expires_at"]) < now_str:
        flash("This password reset link is invalid or expired.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token)

        if password != password_confirm:
            flash("Password confirmation does not match.", "error")
            return render_template("reset_password.html", token=token)

        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), row["user_id"]),
        )
        db.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?",
            (_utc_now_string(), row["id"]),
        )
        db.commit()
        flash("Password updated. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    db = get_db()
    user_id = current_user_id()
    today_str = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    # --- Expense totals ---
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date = ? AND user_id = ?",
        (today_str, user_id)
    ).fetchone()
    today_expense = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date >= ? AND user_id = ?",
        (month_start, user_id)
    ).fetchone()
    month_expense = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    all_time_expense = row["total"]

    # --- Income totals ---
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM income WHERE date = ? AND user_id = ?",
        (today_str, user_id)
    ).fetchone()
    today_income = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM income WHERE date >= ? AND user_id = ?",
        (month_start, user_id)
    ).fetchone()
    month_income = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM income WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    all_time_income = row["total"]

    # Total entries
    row = db.execute("SELECT COUNT(*) AS cnt FROM expenses WHERE user_id = ?", (user_id,)).fetchone()
    total_entries = row["cnt"]

    return render_template(
        "dashboard.html",
        today_expense=today_expense,
        month_expense=month_expense,
        all_time_expense=all_time_expense,
        today_income=today_income,
        month_income=month_income,
        all_time_income=all_time_income,
        total_entries=total_entries,
        today_str=today_str,
    )


@app.route("/expenses")
def expenses_page():
    db = get_db()
    user_id = current_user_id()
    today_str = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date = ? AND user_id = ?",
        (today_str, user_id)
    ).fetchone()
    today_total = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date >= ? AND user_id = ?",
        (month_start, user_id)
    ).fetchone()
    month_total = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    all_time_total = row["total"]

    # Recent expenses grouped by date (last 50 entries)
    expenses = db.execute("""
        SELECT e.id, e.amount, e.description, e.date, e.category_id, e.receipt_image, c.name AS category
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = ?
        ORDER BY e.date DESC, e.created_at DESC
        LIMIT 50
    """, (user_id,)).fetchall()

    grouped = {}
    for exp in expenses:
        d = exp["date"]
        if d not in grouped:
            grouped[d] = []
        grouped[d].append(exp)

    categories = db.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY name",
        (user_id,)
    ).fetchall()

    top_cats = db.execute("""
        SELECT c.name, COALESCE(SUM(e.amount), 0) AS total
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        WHERE e.date >= ? AND e.user_id = ?
        GROUP BY c.name
        ORDER BY total DESC
        LIMIT 5
    """, (month_start, user_id)).fetchall()

    return render_template(
        "expenses.html",
        grouped_expenses=grouped,
        categories=categories,
        top_categories=top_cats,
        today_total=today_total,
        month_total=month_total,
        all_time_total=all_time_total,
        today_str=today_str,
    )


# ---------------------------------------------------------------------------
# Routes — Expenses
# ---------------------------------------------------------------------------

@app.route("/expenses/add", methods=["POST"])
def add_expense():
    user_id = current_user_id()
    amount = request.form.get("amount", "").strip()
    description = request.form.get("description", "").strip()
    expense_date = request.form.get("date", date.today().isoformat()).strip()
    category_id = request.form.get("category_id", "").strip()
    receipt_image = _save_entry_image(request.files.get("receipt_image"))

    if not amount or not category_id:
        flash("Amount and category are required.", "error")
        return redirect(url_for("expenses_page"))

    try:
        amount = float(amount)
        category_id_int = int(category_id)
    except ValueError:
        flash("Invalid amount or category.", "error")
        return redirect(url_for("expenses_page"))

    db = get_db()
    owned_category = db.execute(
        "SELECT id FROM categories WHERE id = ? AND user_id = ?",
        (category_id_int, user_id),
    ).fetchone()
    if owned_category is None:
        flash("Invalid category selection.", "error")
        return redirect(url_for("expenses_page"))

    db.execute(
        "INSERT INTO expenses (user_id, amount, description, date, category_id, receipt_image) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, amount, description, expense_date, category_id_int, receipt_image)
    )
    db.commit()
    flash("Expense added.", "success")
    return redirect(url_for("expenses_page"))


@app.route("/expenses/delete/<int:expense_id>", methods=["POST"])
def delete_expense(expense_id):
    db = get_db()
    user_id = current_user_id()
    redirect_to = request.form.get("redirect_to", "")
    db.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
    db.commit()
    flash("Expense deleted.", "success")
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("expenses_page"))


@app.route("/expenses/edit/<int:expense_id>", methods=["POST"])
def edit_expense(expense_id):
    user_id = current_user_id()
    amount = request.form.get("amount", "").strip()
    description = request.form.get("description", "").strip()
    expense_date = request.form.get("date", "").strip()
    category_id = request.form.get("category_id", "").strip()
    redirect_to = request.form.get("redirect_to", "")

    if not amount or not category_id or not expense_date:
        flash("Amount, date, and category are required.", "error")
        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("expenses_page"))

    try:
        amount = float(amount)
        category_id_int = int(category_id)
    except ValueError:
        flash("Invalid amount or category.", "error")
        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("expenses_page"))

    db = get_db()
    owned_category = db.execute(
        "SELECT id FROM categories WHERE id = ? AND user_id = ?",
        (category_id_int, user_id),
    ).fetchone()
    if owned_category is None:
        flash("Invalid category selection.", "error")
        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("expenses_page"))

    db.execute(
        "UPDATE expenses SET amount = ?, description = ?, date = ?, category_id = ? WHERE id = ? AND user_id = ?",
        (amount, description, expense_date, category_id_int, expense_id, user_id)
    )
    db.commit()
    flash("Expense updated.", "success")
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("expenses_page"))


# ---------------------------------------------------------------------------
# Routes — Income
# ---------------------------------------------------------------------------

@app.route("/income")
def income_page():
    db = get_db()
    user_id = current_user_id()
    today_str = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    # Stats
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM income WHERE date = ? AND user_id = ?",
        (today_str, user_id)
    ).fetchone()
    today_total = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM income WHERE date >= ? AND user_id = ?",
        (month_start, user_id)
    ).fetchone()
    month_total = row["total"]

    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM income WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    all_time_total = row["total"]

    # Recent income entries
    entries = db.execute("""
        SELECT i.id, i.amount, i.description, i.date, i.category_id, i.receipt_image, ic.name AS category
        FROM income i
        JOIN income_categories ic ON i.category_id = ic.id
        WHERE i.user_id = ?
        ORDER BY i.date DESC, i.created_at DESC
        LIMIT 50
    """, (user_id,)).fetchall()

    # Group by date
    grouped = {}
    for entry in entries:
        d = entry["date"]
        if d not in grouped:
            grouped[d] = []
        grouped[d].append(entry)

    # Income categories for the form
    income_cats = db.execute(
        "SELECT id, name FROM income_categories WHERE user_id = ? ORDER BY name",
        (user_id,)
    ).fetchall()

    # Top income sources this month
    top_sources = db.execute("""
        SELECT ic.name, COALESCE(SUM(i.amount), 0) AS total
        FROM income i
        JOIN income_categories ic ON i.category_id = ic.id
        WHERE i.date >= ? AND i.user_id = ?
        GROUP BY ic.name
        ORDER BY total DESC
        LIMIT 5
    """, (month_start, user_id)).fetchall()

    return render_template(
        "income.html",
        today_total=today_total,
        month_total=month_total,
        all_time_total=all_time_total,
        grouped_income=grouped,
        income_categories=income_cats,
        top_sources=top_sources,
        today_str=today_str,
    )


@app.route("/income/add", methods=["POST"])
def add_income():
    user_id = current_user_id()
    amount = request.form.get("amount", "").strip()
    description = request.form.get("description", "").strip()
    income_date = request.form.get("date", date.today().isoformat()).strip()
    category_id = request.form.get("category_id", "").strip()
    receipt_image = _save_entry_image(request.files.get("receipt_image"))

    if not amount or not category_id:
        flash("Amount and source are required.", "error")
        return redirect(url_for("income_page"))

    try:
        amount = float(amount)
        category_id_int = int(category_id)
    except ValueError:
        flash("Invalid amount or source.", "error")
        return redirect(url_for("income_page"))

    db = get_db()
    owned_category = db.execute(
        "SELECT id FROM income_categories WHERE id = ? AND user_id = ?",
        (category_id_int, user_id),
    ).fetchone()
    if owned_category is None:
        flash("Invalid source selection.", "error")
        return redirect(url_for("income_page"))

    db.execute(
        "INSERT INTO income (user_id, amount, description, date, category_id, receipt_image) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, amount, description, income_date, category_id_int, receipt_image)
    )
    db.commit()
    flash("Income added.", "success")
    return redirect(url_for("income_page"))


@app.route("/income/delete/<int:income_id>", methods=["POST"])
def delete_income(income_id):
    db = get_db()
    user_id = current_user_id()
    redirect_to = request.form.get("redirect_to", "")
    db.execute("DELETE FROM income WHERE id = ? AND user_id = ?", (income_id, user_id))
    db.commit()
    flash("Income entry deleted.", "success")
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("income_page"))


@app.route("/income/edit/<int:income_id>", methods=["POST"])
def edit_income(income_id):
    user_id = current_user_id()
    amount = request.form.get("amount", "").strip()
    description = request.form.get("description", "").strip()
    income_date = request.form.get("date", "").strip()
    category_id = request.form.get("category_id", "").strip()
    redirect_to = request.form.get("redirect_to", "")

    if not amount or not category_id or not income_date:
        flash("Amount, date, and source are required.", "error")
        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("income_page"))

    try:
        amount = float(amount)
        category_id_int = int(category_id)
    except ValueError:
        flash("Invalid amount or source.", "error")
        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("income_page"))

    db = get_db()
    owned_category = db.execute(
        "SELECT id FROM income_categories WHERE id = ? AND user_id = ?",
        (category_id_int, user_id),
    ).fetchone()
    if owned_category is None:
        flash("Invalid source selection.", "error")
        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("income_page"))

    db.execute(
        "UPDATE income SET amount = ?, description = ?, date = ?, category_id = ? WHERE id = ? AND user_id = ?",
        (amount, description, income_date, category_id_int, income_id, user_id)
    )
    db.commit()
    flash("Income updated.", "success")
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("income_page"))


# ---------------------------------------------------------------------------
# Routes — Ledger (Daily Overview)
# ---------------------------------------------------------------------------

@app.route("/ledger")
def ledger():
    db = get_db()
    user_id = current_user_id()
    month_filter = request.args.get("month", "")

    # Get all unique dates that have income or expenses
    if month_filter:
        # Filter by month (format: YYYY-MM)
        days = db.execute("""
            SELECT d.date,
                   COALESCE(exp.total, 0) AS expense_total,
                   COALESCE(inc.total, 0) AS income_total,
                   COALESCE(exp.cnt, 0) AS expense_count,
                   COALESCE(inc.cnt, 0) AS income_count
            FROM (
                SELECT date FROM expenses WHERE strftime('%Y-%m', date) = ? AND user_id = ?
                UNION
                SELECT date FROM income WHERE strftime('%Y-%m', date) = ? AND user_id = ?
            ) d
            LEFT JOIN (
                SELECT date, SUM(amount) AS total, COUNT(*) AS cnt FROM expenses WHERE user_id = ? GROUP BY date
            ) exp ON d.date = exp.date
            LEFT JOIN (
                SELECT date, SUM(amount) AS total, COUNT(*) AS cnt FROM income WHERE user_id = ? GROUP BY date
            ) inc ON d.date = inc.date
            ORDER BY d.date DESC
        """, (month_filter, user_id, month_filter, user_id, user_id, user_id)).fetchall()
    else:
        days = db.execute("""
            SELECT d.date,
                   COALESCE(exp.total, 0) AS expense_total,
                   COALESCE(inc.total, 0) AS income_total,
                   COALESCE(exp.cnt, 0) AS expense_count,
                   COALESCE(inc.cnt, 0) AS income_count
            FROM (
                SELECT date FROM expenses WHERE user_id = ?
                UNION
                SELECT date FROM income WHERE user_id = ?
            ) d
            LEFT JOIN (
                SELECT date, SUM(amount) AS total, COUNT(*) AS cnt FROM expenses WHERE user_id = ? GROUP BY date
            ) exp ON d.date = exp.date
            LEFT JOIN (
                SELECT date, SUM(amount) AS total, COUNT(*) AS cnt FROM income WHERE user_id = ? GROUP BY date
            ) inc ON d.date = inc.date
            ORDER BY d.date DESC
        """, (user_id, user_id, user_id, user_id)).fetchall()

    # Available months for the filter dropdown
    available_months = db.execute("""
        SELECT DISTINCT month FROM (
            SELECT strftime('%Y-%m', date) AS month FROM expenses WHERE user_id = ?
            UNION
            SELECT strftime('%Y-%m', date) AS month FROM income WHERE user_id = ?
        )
        ORDER BY month DESC
    """, (user_id, user_id)).fetchall()

    return render_template(
        "ledger.html",
        days=days,
        month_filter=month_filter,
        available_months=available_months,
    )


@app.route("/ledger/<date_str>")
def ledger_day(date_str):
    db = get_db()
    user_id = current_user_id()

    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        flash("Invalid date format.", "error")
        return redirect(url_for("ledger"))

    # Income entries for this date
    income_entries = db.execute("""
        SELECT i.id, i.amount, i.description, i.date, i.category_id, ic.name AS category
        FROM income i
        JOIN income_categories ic ON i.category_id = ic.id
        WHERE i.date = ? AND i.user_id = ?
        ORDER BY i.created_at DESC
    """, (date_str, user_id)).fetchall()

    # Expense entries for this date
    expense_entries = db.execute("""
        SELECT e.id, e.amount, e.description, e.date, e.category_id, c.name AS category
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        WHERE e.date = ? AND e.user_id = ?
        ORDER BY e.created_at DESC
    """, (date_str, user_id)).fetchall()

    # Totals
    income_total = sum(e["amount"] for e in income_entries)
    expense_total = sum(e["amount"] for e in expense_entries)

    # Categories for edit modals
    expense_category_rows = db.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY name",
        (user_id,)
    ).fetchall()
    income_category_rows = db.execute(
        "SELECT id, name FROM income_categories WHERE user_id = ? ORDER BY name",
        (user_id,)
    ).fetchall()

    # Fallback labels for historical rows tied to legacy category records.
    expense_known_ids = {row["id"] for row in expense_category_rows}
    income_known_ids = {row["id"] for row in income_category_rows}
    if any(entry["category_id"] not in expense_known_ids for entry in expense_entries):
        expense_category_rows = list(expense_category_rows)
        expense_category_rows.append({"id": -1, "name": "Uncategorized"})
    if any(entry["category_id"] not in income_known_ids for entry in income_entries):
        income_category_rows = list(income_category_rows)
        income_category_rows.append({"id": -1, "name": "Other"})

    expense_categories = expense_category_rows
    income_categories = income_category_rows

    return render_template(
        "ledger_day.html",
        date_str=date_str,
        income_entries=income_entries,
        expense_entries=expense_entries,
        income_total=income_total,
        expense_total=expense_total,
        expense_categories=expense_categories,
        income_categories=income_categories,
    )


# ---------------------------------------------------------------------------
# Routes — Reports
# ---------------------------------------------------------------------------

@app.route("/reports")
def reports():
    db = get_db()
    user_id = current_user_id()
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")

    results = []
    range_total = 0
    range_income = 0

    if from_date and to_date:
        results = db.execute("""
            SELECT id, amount, description, date, category, type, created_at
            FROM (
                SELECT e.id, e.amount, e.description, e.date, c.name AS category, 'expense' AS type, e.created_at
                FROM expenses e
                JOIN categories c ON e.category_id = c.id
                WHERE e.date >= ? AND e.date <= ? AND e.user_id = ?
                UNION ALL
                SELECT i.id, i.amount, i.description, i.date, ic.name AS category, 'income' AS type, i.created_at
                FROM income i
                JOIN income_categories ic ON i.category_id = ic.id
                WHERE i.date >= ? AND i.date <= ? AND i.user_id = ?
            )
            ORDER BY date DESC, created_at DESC
        """, (from_date, to_date, user_id, from_date, to_date, user_id)).fetchall()

        row = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date >= ? AND date <= ? AND user_id = ?",
            (from_date, to_date, user_id)
        ).fetchone()
        range_total = row["total"]

        row = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM income WHERE date >= ? AND date <= ? AND user_id = ?",
            (from_date, to_date, user_id)
        ).fetchone()
        range_income = row["total"]

    # Monthly breakdown (last 12 months) — expenses
    monthly_expenses = db.execute("""
        SELECT strftime('%Y-%m', date) AS month, SUM(amount) AS total
        FROM expenses
        WHERE user_id = ?
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """, (user_id,)).fetchall()

    # Monthly breakdown — income
    monthly_income = db.execute("""
        SELECT strftime('%Y-%m', date) AS month, SUM(amount) AS total
        FROM income
        WHERE user_id = ?
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """, (user_id,)).fetchall()

    # Merge monthly data
    income_map = {m["month"]: m["total"] for m in monthly_income}
    months_set = set()
    for m in monthly_expenses:
        months_set.add(m["month"])
    for m in monthly_income:
        months_set.add(m["month"])

    expense_map = {m["month"]: m["total"] for m in monthly_expenses}
    monthly_merged = []
    for month in sorted(months_set, reverse=True)[:12]:
        monthly_merged.append({
            "month": month,
            "expense": expense_map.get(month, 0),
            "income": income_map.get(month, 0),
        })

    # Category breakdown (all time)
    by_category = db.execute("""
        SELECT c.name, COALESCE(SUM(e.amount), 0) AS total, COUNT(e.id) AS count
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = ?
        GROUP BY c.name
        ORDER BY total DESC
    """, (user_id,)).fetchall()

    # Income source breakdown (all time)
    by_source = db.execute("""
        SELECT ic.name, COALESCE(SUM(i.amount), 0) AS total, COUNT(i.id) AS count
        FROM income i
        JOIN income_categories ic ON i.category_id = ic.id
        WHERE i.user_id = ?
        GROUP BY ic.name
        ORDER BY total DESC
    """, (user_id,)).fetchall()

    # Daily average this month
    month_start = date.today().replace(day=1).isoformat()
    row = db.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total,
               COUNT(DISTINCT date) AS days
        FROM expenses WHERE date >= ? AND user_id = ?
    """, (month_start, user_id)).fetchone()
    daily_avg = row["total"] / max(row["days"], 1)

    return render_template(
        "reports.html",
        from_date=from_date,
        to_date=to_date,
        results=results,
        range_total=range_total,
        range_income=range_income,
        monthly=monthly_merged,
        by_category=by_category,
        by_source=by_source,
        daily_avg=daily_avg,
    )


# ---------------------------------------------------------------------------
# Routes — Categories (Expense)
# ---------------------------------------------------------------------------

@app.route("/categories")
def categories():
    db = get_db()
    user_id = current_user_id()
    cats = db.execute("""
        SELECT c.id, c.name, c.created_at,
               COUNT(e.id) AS expense_count,
               COALESCE(SUM(e.amount), 0) AS total_amount
        FROM categories c
        LEFT JOIN expenses e ON c.id = e.category_id AND e.user_id = ?
        WHERE c.user_id = ?
        GROUP BY c.id
        ORDER BY c.name
    """, (user_id, user_id)).fetchall()

    # Income categories
    income_cats = db.execute("""
        SELECT ic.id, ic.name, ic.created_at,
               COUNT(i.id) AS income_count,
               COALESCE(SUM(i.amount), 0) AS total_amount
        FROM income_categories ic
        LEFT JOIN income i ON ic.id = i.category_id AND i.user_id = ?
        WHERE ic.user_id = ?
        GROUP BY ic.id
        ORDER BY ic.name
    """, (user_id, user_id)).fetchall()

    return render_template("categories.html", categories=cats, income_categories=income_cats)


@app.route("/categories/add", methods=["POST"])
def add_category():
    user_id = current_user_id()
    name = request.form.get("name", "").strip()
    if not name:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "error": "Category name is required."}), 400
        flash("Category name is required.", "error")
        return redirect(url_for("categories"))

    db = get_db()
    try:
        cursor = db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, name))
        db.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            row = db.execute("SELECT id, name FROM categories WHERE id = ? AND user_id = ?", (cursor.lastrowid, user_id)).fetchone()
            return jsonify({"ok": True, "id": row["id"], "name": row["name"]})
        flash(f"Category '{name}' added.", "success")
    except sqlite3.IntegrityError:
        if request.headers.get("X-Requested-With") == "fetch":
            row = db.execute("SELECT id, name FROM categories WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
            return jsonify({"ok": True, "id": row["id"], "name": row["name"], "existing": True})
        flash(f"Category '{name}' already exists.", "error")
    return redirect(url_for("categories"))


@app.route("/categories/delete/<int:cat_id>", methods=["POST"])
def delete_category(cat_id):
    db = get_db()
    user_id = current_user_id()

    owned = db.execute(
        "SELECT id FROM categories WHERE id = ? AND user_id = ?",
        (cat_id, user_id),
    ).fetchone()
    if owned is None:
        flash("Category not found.", "error")
        return redirect(url_for("categories"))

    # Check if category has expenses
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM expenses WHERE category_id = ? AND user_id = ?", (cat_id, user_id)
    ).fetchone()

    if row["cnt"] > 0:
        # Reassign to "Uncategorized" — create it if needed
        unc = db.execute(
            "SELECT id FROM categories WHERE user_id = ? AND name = 'Uncategorized'",
            (user_id,),
        ).fetchone()
        if unc is None:
            db.execute("INSERT INTO categories (user_id, name) VALUES (?, 'Uncategorized')", (user_id,))
            db.commit()
            unc = db.execute(
                "SELECT id FROM categories WHERE user_id = ? AND name = 'Uncategorized'",
                (user_id,),
            ).fetchone()
        db.execute(
            "UPDATE expenses SET category_id = ? WHERE category_id = ? AND user_id = ?",
            (unc["id"], cat_id, user_id)
        )

    db.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id))
    db.commit()
    flash("Category deleted.", "success")
    return redirect(url_for("categories"))


# ---------------------------------------------------------------------------
# Routes — Income Categories
# ---------------------------------------------------------------------------

@app.route("/income-categories/add", methods=["POST"])
def add_income_category():
    user_id = current_user_id()
    name = request.form.get("name", "").strip()
    if not name:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "error": "Category name is required."}), 400
        flash("Category name is required.", "error")
        return redirect(url_for("categories"))

    db = get_db()
    try:
        cursor = db.execute("INSERT INTO income_categories (user_id, name) VALUES (?, ?)", (user_id, name))
        db.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            row = db.execute("SELECT id, name FROM income_categories WHERE id = ? AND user_id = ?", (cursor.lastrowid, user_id)).fetchone()
            return jsonify({"ok": True, "id": row["id"], "name": row["name"]})
        flash(f"Income source '{name}' added.", "success")
    except sqlite3.IntegrityError:
        if request.headers.get("X-Requested-With") == "fetch":
            row = db.execute("SELECT id, name FROM income_categories WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
            return jsonify({"ok": True, "id": row["id"], "name": row["name"], "existing": True})
        flash(f"Income source '{name}' already exists.", "error")
    return redirect(url_for("categories"))


@app.route("/income-categories/delete/<int:cat_id>", methods=["POST"])
def delete_income_category(cat_id):
    db = get_db()
    user_id = current_user_id()

    owned = db.execute(
        "SELECT id FROM income_categories WHERE id = ? AND user_id = ?",
        (cat_id, user_id),
    ).fetchone()
    if owned is None:
        flash("Income source not found.", "error")
        return redirect(url_for("categories"))

    # Check if category has income entries
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM income WHERE category_id = ? AND user_id = ?", (cat_id, user_id)
    ).fetchone()

    if row["cnt"] > 0:
        # Reassign to "Other" — create it if needed
        unc = db.execute(
            "SELECT id FROM income_categories WHERE user_id = ? AND name = 'Other'",
            (user_id,),
        ).fetchone()
        if unc is None:
            db.execute("INSERT INTO income_categories (user_id, name) VALUES (?, 'Other')", (user_id,))
            db.commit()
            unc = db.execute(
                "SELECT id FROM income_categories WHERE user_id = ? AND name = 'Other'",
                (user_id,),
            ).fetchone()
        db.execute(
            "UPDATE income SET category_id = ? WHERE category_id = ? AND user_id = ?",
            (unc["id"], cat_id, user_id)
        )

    db.execute("DELETE FROM income_categories WHERE id = ? AND user_id = ?", (cat_id, user_id))
    db.commit()
    flash("Income source deleted.", "success")
    return redirect(url_for("categories"))


# ---------------------------------------------------------------------------
# Routes — Wishlist
# ---------------------------------------------------------------------------

@app.route("/wishlist")
def wishlist():
    db = get_db()
    user_id = current_user_id()
    active = db.execute(
        "SELECT * FROM wishlist WHERE purchased = 0 AND user_id = ? ORDER BY "
        "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC"
    , (user_id,)).fetchall()
    purchased = db.execute(
        "SELECT * FROM wishlist WHERE purchased = 1 AND user_id = ? ORDER BY purchased_at DESC",
        (user_id,)
    ).fetchall()

    # Stats
    total_wishlist = sum(item["price"] for item in active)
    total_purchased = sum(item["price"] for item in purchased)

    return render_template(
        "wishlist.html",
        active=active,
        purchased=purchased,
        total_wishlist=total_wishlist,
        total_purchased=total_purchased,
    )


@app.route("/wishlist/add", methods=["POST"])
def add_wishlist():
    user_id = current_user_id()
    name = request.form.get("name", "").strip()
    price = request.form.get("price", "0").strip()
    priority = request.form.get("priority", "medium").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Item name is required.", "error")
        return redirect(url_for("wishlist"))

    try:
        price = float(price)
    except ValueError:
        price = 0

    db = get_db()
    db.execute(
        "INSERT INTO wishlist (user_id, name, price, priority, notes) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, price, priority, notes)
    )
    db.commit()
    flash(f"'{name}' added to wishlist.", "success")
    return redirect(url_for("wishlist"))


@app.route("/wishlist/edit/<int:item_id>", methods=["POST"])
def edit_wishlist(item_id):
    user_id = current_user_id()
    name = request.form.get("name", "").strip()
    price = request.form.get("price", "0").strip()
    priority = request.form.get("priority", "medium").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Item name is required.", "error")
        return redirect(url_for("wishlist"))

    try:
        price = float(price)
    except ValueError:
        price = 0

    db = get_db()
    db.execute(
        "UPDATE wishlist SET name = ?, price = ?, priority = ?, notes = ? WHERE id = ? AND user_id = ?",
        (name, price, priority, notes, item_id, user_id)
    )
    db.commit()
    flash(f"'{name}' updated.", "success")
    return redirect(url_for("wishlist"))


@app.route("/wishlist/purchase/<int:item_id>", methods=["POST"])
def purchase_wishlist(item_id):
    db = get_db()
    user_id = current_user_id()
    db.execute(
        "UPDATE wishlist SET purchased = 1, purchased_at = datetime('now','localtime') WHERE id = ? AND user_id = ?",
        (item_id, user_id)
    )
    db.commit()
    flash("Item marked as purchased.", "success")
    return redirect(url_for("wishlist"))


@app.route("/wishlist/delete/<int:item_id>", methods=["POST"])
def delete_wishlist(item_id):
    db = get_db()
    user_id = current_user_id()
    db.execute("DELETE FROM wishlist WHERE id = ? AND user_id = ?", (item_id, user_id))
    db.commit()
    flash("Wishlist item deleted.", "success")
    return redirect(url_for("wishlist"))


# ---------------------------------------------------------------------------
# API — for AJAX calls (chart data)
# ---------------------------------------------------------------------------


@app.route("/api/recognize-transaction-photo", methods=["POST"])
def recognize_transaction_photo():
    """Analyze a transaction photo and return structured draft fields."""
    if genai is None or types is None:
        return jsonify({
            "ok": False,
            "error": "Google GenAI SDK is not installed. Add 'google-genai' to requirements and install dependencies."
        }), 500

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return jsonify({
            "ok": False,
            "error": "Missing GEMINI_API_KEY environment variable."
        }), 400

    file = request.files.get("photo")
    if file is None or not file.filename:
        return jsonify({"ok": False, "error": "Photo file is required."}), 400

    mime_type = (file.mimetype or "").strip().lower()
    if mime_type not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
        return jsonify({
            "ok": False,
            "error": "Unsupported image type. Use JPG, PNG, or WEBP."
        }), 400

    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"ok": False, "error": "Uploaded file is empty."}), 400
    if len(image_bytes) > 8 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Image is too large (max 8MB)."}), 400

    target = request.form.get("target", "").strip().lower()
    if target not in {"expense", "income", "auto"}:
        target = "auto"

    db = get_db()
    user_id = current_user_id()

    tx_type = "income" if target == "income" else "expense"
    today_default = datetime.now().strftime("%Y-%m-%d")

    system_prompt = f"""
Analyze this {'receipt/invoice' if tx_type == 'expense' else 'payment proof/transfer screenshot'} image.
Extract financial transaction data and return ONLY a valid JSON array, no markdown, no explanation.
Format:
[{{"date":"YYYY-MM-DD","vendor":"name","amount":numeric_only,"category":"category label inferred from the receipt","description":"short description","items":[{{"name":"item","amount":numeric_optional}}]}}]
- amount must be a plain number (no Rp, no commas)
- Prioritize the final payable total and not line-item prices (e.g. GRAND TOTAL, TOTAL BAYAR, AMOUNT DUE)
- If date is unclear use today: {today_default}
- Category rules:
  - infer the most fitting category from the receipt itself
  - do not rely on any existing category list
  - if category is unclear, return a short sensible category guess anyway
- Description rules:
    - for expense, describe purchased objects/items, not the store name
    - if there are multiple objects, use the most expensive one or two items, then append "and others"
    - keep it specific (for example: "Salmon and shrimp and others")
    - keep it concise and useful for an expense ledger
- Return one object for the total, not per line item
""".strip()

    user_prompt = (
        f"target={target}\n"
        "infer_categories_independently=true"
    )

    # Some models (for example Gemma chat models) do not support developer/system instructions.
    # In that case we prepend the rules into the user content instead.
    request_prompt = (
        system_prompt + "\n\n" + user_prompt
        if GEMINI_MODEL.startswith("gemma-") else user_prompt
    )

    try:
        client = genai.Client(api_key=api_key)
        config_kwargs = {
            "system_instruction": None if GEMINI_MODEL.startswith("gemma-") else system_prompt,
            "temperature": 0.1,
        }
        if not GEMINI_MODEL.startswith("gemma-"):
            config_kwargs["response_mime_type"] = "application/json"

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                request_prompt,
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(**config_kwargs),
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Gemini request failed: {exc}"}), 500

    raw_response_text = getattr(response, "text", "")
    parsed = _extract_json_object(raw_response_text)
    if not isinstance(parsed, dict):
        parsed_arr = _extract_json_array(raw_response_text)
        if isinstance(parsed_arr, list) and parsed_arr and isinstance(parsed_arr[0], dict):
            parsed = parsed_arr[0]
    if not isinstance(parsed, dict):
        parsed = _extract_transaction_fields_from_text(raw_response_text)
    if not isinstance(parsed, dict):
        parsed = {}

    if not any([
        parsed.get("amount"),
        parsed.get("store_name"),
        parsed.get("date"),
        parsed.get("category_name"),
        parsed.get("description"),
    ]):
        return jsonify({
            "ok": False,
            "error": "Could not parse model response.",
            "raw_response": (raw_response_text or "")[:400],
        }), 500

    amount_val = _coerce_amount(parsed.get("amount", 0))
    if amount_val <= 0:
        amount_val = _best_guess_total_from_text(raw_response_text)

    date_val = str(parsed.get("date", "")).strip()
    if date_val:
        try:
            datetime.strptime(date_val, "%Y-%m-%d")
        except ValueError:
            date_val = ""

    store_name_val = str(parsed.get("store_name") or parsed.get("vendor") or "").strip()
    description_val = str(parsed.get("description") or "").strip()
    category_name_raw = str(parsed.get("category_name") or parsed.get("category") or "").strip()

    def _shorten_text(text, max_len=42):
        value = str(text or "").strip()
        if len(value) <= max_len:
            return value

        words = value.split()
        compact_words = []
        for word in words:
            candidate = " ".join(compact_words + [word])
            if len(candidate) > max_len:
                break
            compact_words.append(word)

        if compact_words:
            return " ".join(compact_words)
        return value[:max_len].rstrip()

    def _clean_item_label(label):
        cleaned = re.sub(r'(?i)^\s*(?:and|dan|&|\+)\s+', '', str(label or '').strip())
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .,-')
        return _shorten_text(cleaned, max_len=24)

    def _generalize_description(description_text, category_name):
        text = str(description_text or "").strip()
        if not text:
            return ""

        lowered = text.lower()
        has_multi_item_cues = any(token in lowered for token in [",", ";", " / ", " and ", " & ", " + "])
        if not has_multi_item_cues:
            return text

        parts = re.split(r'\s*(?:,|;|/|\band\b|&|\+)\s*', text, flags=re.IGNORECASE)
        cleaned_parts = []
        seen = set()
        for part in parts:
            cleaned = re.sub(
                r'(?i)\b\d+(?:[.,]\d+)?\s*(?:x|pcs?|pc|pack|packs|kg|g|gr|gram|ml|l|ltr|btl|bottle|botol|item|items)\b',
                '',
                part,
            )
            cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .,-')
            if not cleaned:
                continue

            normalized = cleaned.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned_parts.append(cleaned)

        if len(cleaned_parts) >= 3:
            return f"{cleaned_parts[0]} and {cleaned_parts[1]} and others"
        if len(cleaned_parts) == 2:
            return f"{cleaned_parts[0]} and {cleaned_parts[1]}"
        if len(cleaned_parts) == 1:
            return cleaned_parts[0]

        category = str(category_name or "").strip()
        if category:
            return f"{category} purchases"
        return "Mixed purchases"

    # Normalize model output in case it still returns item lists split by commas.
    if description_val:
        description_val = _generalize_description(description_val, category_name_raw)
        description_val = _shorten_text(description_val, max_len=42)

    def _collect_items(raw_items):
        if not isinstance(raw_items, list):
            return []

        def _infer_amount_from_name(name_text):
            text = str(name_text or "")
            if not text:
                return 0.0

            # Capture numeric tokens from item text (e.g. "Tea 350ml 12000").
            candidates = re.findall(r'\d+(?:[.,]\d+)?', text)
            if not candidates:
                return 0.0

            values = []
            for token in candidates:
                amount_guess = _coerce_amount(token)
                if amount_guess > 0:
                    values.append(amount_guess)

            if not values:
                return 0.0

            # Heuristic: highest positive number in the label is most likely line price.
            return max(values)

        collected = []
        seen = set()
        for item in raw_items:
            if isinstance(item, str):
                name = item.strip()
                amount = _infer_amount_from_name(name)
            elif isinstance(item, dict):
                name = str(
                    item.get("name")
                    or item.get("item")
                    or item.get("description")
                    or item.get("product")
                    or ""
                ).strip()
                amount = _coerce_amount(
                    item.get("amount")
                    or item.get("price")
                    or item.get("line_total")
                    or item.get("item_total")
                    or item.get("linePrice")
                    or item.get("unit_price")
                    or item.get("unitPrice")
                    or item.get("total")
                    or item.get("subtotal")
                    or 0
                )
                if amount <= 0:
                    amount = _infer_amount_from_name(name)
            else:
                name = ""
                amount = 0.0

            if not name:
                continue
            normalized = name.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            collected.append({"name": name, "amount": amount})

        return collected

    def _format_top_items_description(items):
        if not items:
            return ""

        sortable = list(items)
        sortable.sort(key=lambda entry: float(entry.get("amount") or 0), reverse=True)
        top_names = [_clean_item_label(entry.get("name", "")) for entry in sortable if entry.get("name")]
        top_names = [name for name in top_names if name]

        if not top_names:
            return ""
        if len(top_names) == 1:
            return _shorten_text(top_names[0], max_len=42)
        if len(top_names) == 2:
            return _shorten_text(f"{top_names[0]} and {top_names[1]}", max_len=42)
        return _shorten_text(f"{top_names[0]} and {top_names[1]} and others", max_len=42)

    parsed_items = _collect_items(
        parsed.get("items") or parsed.get("line_items") or parsed.get("products")
    )

    if parsed_items:
        description_val = _format_top_items_description(parsed_items)

    if description_val:
        description_val = _shorten_text(description_val, max_len=42)

    category_name_val = category_name_raw

    # Drop generic guesses like Minuman unless the model text itself supports them.
    if category_name_val and _is_generic_category(category_name_val):
        if not _category_supported_by_text(category_name_val, raw_response_text):
            category_name_val = ""

    category_exists = bool(category_name_val and not _is_generic_category(category_name_val))

    if category_name_val and _is_generic_category(category_name_val):
        category_name_val = ""
        category_exists = False

    likely_category_match = _guess_likely_category_match(
        store_name_val,
        category_name_raw,
        raw_response_text,
        tx_type,
    )
    if not likely_category_match and category_name_raw and not _is_generic_category(category_name_raw):
        likely_category_match = category_name_raw
    if not likely_category_match and store_name_val:
        likely_category_match = store_name_val

    # Check if the likely category already exists in the database
    likely_category_exists = False
    if likely_category_match:
        likely_norm = _normalize_text(likely_category_match)
        if tx_type == "income":
            rows = db.execute("SELECT name FROM income_categories WHERE user_id = ?", (user_id,)).fetchall()
        else:
            rows = db.execute("SELECT name FROM categories WHERE user_id = ?", (user_id,)).fetchall()
        likely_category_exists = any(_normalize_text(row["name"]) == likely_norm for row in rows)

    if category_name_val:
        category_prompt = ""
    elif category_name_raw:
        if _is_generic_category(category_name_raw):
            category_prompt = "Category was too generic. Please choose a more specific category manually."
        else:
            category_prompt = f'Category "{category_name_raw}" is not in your list yet. Add it in Categories.'
    else:
        category_prompt = "Could not infer a category. Please choose one manually."

    return jsonify({
        "ok": True,
        "amount": amount_val,
        "store_name": store_name_val,
        "description": description_val,
        "date": date_val,
        "category_name": category_name_val,
        "category_name_raw": category_name_raw,
        "likely_category_match": likely_category_match,
        "category_exists": category_exists,
        "suggest_add_category": bool(likely_category_match and not likely_category_exists),
        "category_prompt": category_prompt,
    })


@app.route("/api/daily-expenses")
def api_daily_expenses():
    """Return last 30 days of daily expense totals for charting."""
    db = get_db()
    user_id = current_user_id()
    rows = db.execute("""
        SELECT date, SUM(amount) AS total
        FROM expenses
        WHERE date >= date('now', '-30 days', 'localtime') AND user_id = ?
        GROUP BY date
        ORDER BY date
    """, (user_id,)).fetchall()
    return jsonify([{"date": r["date"], "total": r["total"]} for r in rows])


@app.route("/api/category-breakdown")
def api_category_breakdown():
    """Return category totals for the current month."""
    month_start = date.today().replace(day=1).isoformat()
    db = get_db()
    user_id = current_user_id()
    rows = db.execute("""
        SELECT c.name, COALESCE(SUM(e.amount), 0) AS total
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        WHERE e.date >= ? AND e.user_id = ?
        GROUP BY c.name
        ORDER BY total DESC
    """, (month_start, user_id)).fetchall()
    return jsonify([{"name": r["name"], "total": r["total"]} for r in rows])


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
