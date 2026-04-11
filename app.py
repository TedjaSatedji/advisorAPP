"""
advisorAPP — A refined financial tracking application.
Flask + SQLite + Jinja2. Dark-mode editorial dashboard.
"""

import sqlite3
import os
from datetime import datetime, date
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, g
)

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "advisor.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "advisor-app-secret-key-change-me")

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
        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            amount      REAL    NOT NULL,
            description TEXT    NOT NULL DEFAULT '',
            date        TEXT    NOT NULL,
            category_id INTEGER NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS wishlist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            price       REAL    NOT NULL DEFAULT 0,
            priority    TEXT    NOT NULL DEFAULT 'medium',
            notes       TEXT    DEFAULT '',
            purchased   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            purchased_at TEXT   DEFAULT NULL
        );
    """)

    # Seed default categories
    cursor = db.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0:
        db.execute("INSERT INTO categories (name) VALUES ('Makanan')")
        db.execute("INSERT INTO categories (name) VALUES ('Minuman')")
        db.commit()

    db.close()


# ---------------------------------------------------------------------------
# Template context helpers
# ---------------------------------------------------------------------------

@app.context_processor
def inject_now():
    return {"now": datetime.now(), "today": date.today().isoformat()}


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    db = get_db()
    today_str = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    # Today's total
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date = ?",
        (today_str,)
    ).fetchone()
    today_total = row["total"]

    # This month's total
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date >= ?",
        (month_start,)
    ).fetchone()
    month_total = row["total"]

    # All-time total
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses"
    ).fetchone()
    all_time_total = row["total"]

    # Total entries
    row = db.execute("SELECT COUNT(*) AS cnt FROM expenses").fetchone()
    total_entries = row["cnt"]

    # Recent expenses grouped by date (last 30 entries)
    expenses = db.execute("""
        SELECT e.id, e.amount, e.description, e.date, c.name AS category
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        ORDER BY e.date DESC, e.created_at DESC
        LIMIT 50
    """).fetchall()

    # Group by date
    grouped = {}
    for exp in expenses:
        d = exp["date"]
        if d not in grouped:
            grouped[d] = []
        grouped[d].append(exp)

    # Categories for the add-expense form
    categories = db.execute(
        "SELECT id, name FROM categories ORDER BY name"
    ).fetchall()

    # Top categories this month
    top_cats = db.execute("""
        SELECT c.name, COALESCE(SUM(e.amount), 0) AS total
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        WHERE e.date >= ?
        GROUP BY c.name
        ORDER BY total DESC
        LIMIT 5
    """, (month_start,)).fetchall()

    return render_template(
        "dashboard.html",
        today_total=today_total,
        month_total=month_total,
        all_time_total=all_time_total,
        total_entries=total_entries,
        grouped_expenses=grouped,
        categories=categories,
        top_categories=top_cats,
        today_str=today_str,
    )


# ---------------------------------------------------------------------------
# Routes — Expenses
# ---------------------------------------------------------------------------

@app.route("/expenses/add", methods=["POST"])
def add_expense():
    amount = request.form.get("amount", "").strip()
    description = request.form.get("description", "").strip()
    expense_date = request.form.get("date", date.today().isoformat()).strip()
    category_id = request.form.get("category_id", "").strip()

    if not amount or not category_id:
        flash("Amount and category are required.", "error")
        return redirect(url_for("dashboard"))

    try:
        amount = float(amount)
    except ValueError:
        flash("Invalid amount.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    db.execute(
        "INSERT INTO expenses (amount, description, date, category_id) VALUES (?, ?, ?, ?)",
        (amount, description, expense_date, int(category_id))
    )
    db.commit()
    flash("Expense added.", "success")
    return redirect(url_for("dashboard"))


@app.route("/expenses/delete/<int:expense_id>", methods=["POST"])
def delete_expense(expense_id):
    db = get_db()
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes — Reports
# ---------------------------------------------------------------------------

@app.route("/reports")
def reports():
    db = get_db()
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")

    results = []
    range_total = 0

    if from_date and to_date:
        results = db.execute("""
            SELECT e.id, e.amount, e.description, e.date, c.name AS category
            FROM expenses e
            JOIN categories c ON e.category_id = c.id
            WHERE e.date >= ? AND e.date <= ?
            ORDER BY e.date DESC, e.created_at DESC
        """, (from_date, to_date)).fetchall()

        row = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE date >= ? AND date <= ?",
            (from_date, to_date)
        ).fetchone()
        range_total = row["total"]

    # Monthly breakdown (last 12 months)
    monthly = db.execute("""
        SELECT strftime('%Y-%m', date) AS month, SUM(amount) AS total
        FROM expenses
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """).fetchall()

    # Category breakdown (all time)
    by_category = db.execute("""
        SELECT c.name, COALESCE(SUM(e.amount), 0) AS total, COUNT(e.id) AS count
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        GROUP BY c.name
        ORDER BY total DESC
    """).fetchall()

    # Daily average this month
    month_start = date.today().replace(day=1).isoformat()
    today_str = date.today().isoformat()
    row = db.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total,
               COUNT(DISTINCT date) AS days
        FROM expenses WHERE date >= ?
    """, (month_start,)).fetchone()
    daily_avg = row["total"] / max(row["days"], 1)

    return render_template(
        "reports.html",
        from_date=from_date,
        to_date=to_date,
        results=results,
        range_total=range_total,
        monthly=monthly,
        by_category=by_category,
        daily_avg=daily_avg,
    )


# ---------------------------------------------------------------------------
# Routes — Categories
# ---------------------------------------------------------------------------

@app.route("/categories")
def categories():
    db = get_db()
    cats = db.execute("""
        SELECT c.id, c.name, c.created_at,
               COUNT(e.id) AS expense_count,
               COALESCE(SUM(e.amount), 0) AS total_amount
        FROM categories c
        LEFT JOIN expenses e ON c.id = e.category_id
        GROUP BY c.id
        ORDER BY c.name
    """).fetchall()
    return render_template("categories.html", categories=cats)


@app.route("/categories/add", methods=["POST"])
def add_category():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Category name is required.", "error")
        return redirect(url_for("categories"))

    db = get_db()
    try:
        db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        db.commit()
        flash(f"Category '{name}' added.", "success")
    except sqlite3.IntegrityError:
        flash(f"Category '{name}' already exists.", "error")
    return redirect(url_for("categories"))


@app.route("/categories/delete/<int:cat_id>", methods=["POST"])
def delete_category(cat_id):
    db = get_db()

    # Check if category has expenses
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM expenses WHERE category_id = ?", (cat_id,)
    ).fetchone()

    if row["cnt"] > 0:
        # Reassign to "Uncategorized" — create it if needed
        unc = db.execute(
            "SELECT id FROM categories WHERE name = 'Uncategorized'"
        ).fetchone()
        if unc is None:
            db.execute("INSERT INTO categories (name) VALUES ('Uncategorized')")
            db.commit()
            unc = db.execute(
                "SELECT id FROM categories WHERE name = 'Uncategorized'"
            ).fetchone()
        db.execute(
            "UPDATE expenses SET category_id = ? WHERE category_id = ?",
            (unc["id"], cat_id)
        )

    db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    db.commit()
    flash("Category deleted.", "success")
    return redirect(url_for("categories"))


# ---------------------------------------------------------------------------
# Routes — Wishlist
# ---------------------------------------------------------------------------

@app.route("/wishlist")
def wishlist():
    db = get_db()
    active = db.execute(
        "SELECT * FROM wishlist WHERE purchased = 0 ORDER BY "
        "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC"
    ).fetchall()
    purchased = db.execute(
        "SELECT * FROM wishlist WHERE purchased = 1 ORDER BY purchased_at DESC"
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
        "INSERT INTO wishlist (name, price, priority, notes) VALUES (?, ?, ?, ?)",
        (name, price, priority, notes)
    )
    db.commit()
    flash(f"'{name}' added to wishlist.", "success")
    return redirect(url_for("wishlist"))


@app.route("/wishlist/purchase/<int:item_id>", methods=["POST"])
def purchase_wishlist(item_id):
    db = get_db()
    db.execute(
        "UPDATE wishlist SET purchased = 1, purchased_at = datetime('now','localtime') WHERE id = ?",
        (item_id,)
    )
    db.commit()
    flash("Item marked as purchased.", "success")
    return redirect(url_for("wishlist"))


@app.route("/wishlist/delete/<int:item_id>", methods=["POST"])
def delete_wishlist(item_id):
    db = get_db()
    db.execute("DELETE FROM wishlist WHERE id = ?", (item_id,))
    db.commit()
    flash("Wishlist item deleted.", "success")
    return redirect(url_for("wishlist"))


# ---------------------------------------------------------------------------
# API — for AJAX calls (chart data, etc.)
# ---------------------------------------------------------------------------

@app.route("/api/daily-expenses")
def api_daily_expenses():
    """Return last 30 days of daily expense totals for charting."""
    db = get_db()
    rows = db.execute("""
        SELECT date, SUM(amount) AS total
        FROM expenses
        WHERE date >= date('now', '-30 days', 'localtime')
        GROUP BY date
        ORDER BY date
    """).fetchall()
    return jsonify([{"date": r["date"], "total": r["total"]} for r in rows])


@app.route("/api/category-breakdown")
def api_category_breakdown():
    """Return category totals for the current month."""
    month_start = date.today().replace(day=1).isoformat()
    db = get_db()
    rows = db.execute("""
        SELECT c.name, COALESCE(SUM(e.amount), 0) AS total
        FROM expenses e
        JOIN categories c ON e.category_id = c.id
        WHERE e.date >= ?
        GROUP BY c.name
        ORDER BY total DESC
    """, (month_start,)).fetchall()
    return jsonify([{"name": r["name"], "total": r["total"]} for r in rows])


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
