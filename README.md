# XPense

A refined, editorial financial tracking dashboard built with Flask + SQLite.

> Bloomberg terminal meets modern fintech — dense but clean.

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file (or export these variables in your shell):

```bash
SECRET_KEY=replace-with-a-long-random-secret
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemma-3-27b-it
SESSION_COOKIE_SECURE=0

APP_BASE_URL=http://localhost:5000

MAIL_SMTP_HOST=smtp.gmail.com
MAIL_SMTP_PORT=587
MAIL_SMTP_USER=youremail@gmail.com
MAIL_SMTP_PASSWORD=your-gmail-app-password
MAIL_SMTP_USE_TLS=1
MAIL_SMTP_TIMEOUT_SECONDS=15
MAIL_FROM_EMAIL=youremail@gmail.com
MAIL_FROM_NAME=XPense Support
MAIL_PASSWORD_RESET_SUBJECT=XPense Password Reset
MAIL_PASSWORD_RESET_EXPIRY_MINUTES=60
MAIL_DEBUG_SHOW_RESET_LINK=0
```

`SECRET_KEY` is required. `GEMINI_API_KEY` is only required for photo scanning.

### Gmail SMTP setup (for forgot-password emails)

1. Enable 2-Step Verification on your Google account.
2. Generate a Google App Password (Mail).
3. Set `MAIL_SMTP_USER` to your Gmail address.
4. Set `MAIL_SMTP_PASSWORD` to the generated app password (not your normal Gmail password).
5. Set `APP_BASE_URL` to your public URL in production (for example `https://yourdomain.com`).

### 3. Run (development)

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000)

### 3. Run (production with Gunicorn)

```bash
gunicorn -c gunicorn.conf.py app:app
```

For production, set `SESSION_COOKIE_SECURE=1` and serve over HTTPS.

## Features

- **User Authentication** — Register, login, logout with password hashing
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
