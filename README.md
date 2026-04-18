# XPense

A refined, editorial financial tracking dashboard built with Flask + SQLite.

> Bloomberg terminal meets modern fintech — dense but clean.

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run (development)

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000)

### 3. Run (production with Gunicorn)

```bash
gunicorn -c gunicorn.conf.py app:app
```

## Features

- **Daily Expense Tracking** — Add, view, and delete expenses with categories
- **Financial Reports** — Date range queries, monthly totals, category breakdown
- **Category Management** — Add/delete categories with smart reassignment
- **Wishlist** — Track desired purchases with priority levels

## Tech Stack

| Layer     | Technology              |
|-----------|-------------------------|
| Backend   | Python 3.10+, Flask     |
| Templates | Jinja2                  |
| Database  | SQLite (stdlib)         |
| CSS       | Tailwind (pre-compiled) |
| Server    | Gunicorn (4 workers)    |

## Project Structure

```
XPense/
├── app.py                  # Flask application
├── gunicorn.conf.py        # Production server config
├── requirements.txt        # Python dependencies
├── tailwind.config.js      # Tailwind source config
├── tailwind.input.css      # Tailwind source CSS
├── xpense.db               # SQLite database (auto-created)
├── static/
│   └── tailwind.output.css # Pre-compiled CSS
└── templates/
    ├── base.html           # Base layout
    ├── dashboard.html      # Main dashboard
    ├── reports.html        # Financial reports
    ├── categories.html     # Category management
    └── wishlist.html       # Wishlist page
```

## Database

The database initializes automatically on first run with default categories:
- **Makanan** (Food)
- **Minuman** (Drinks)
